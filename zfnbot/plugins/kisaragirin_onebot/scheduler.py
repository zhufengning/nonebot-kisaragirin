from __future__ import annotations

import asyncio
import math
import random
import time

from nonebot import get_bot, logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .config import PLUGIN_CONFIG
from .payload import build_agent_request
from .state import (
    GroupState,
    QueuedMessage,
    _begin_reply_run,
    _get_group_agent,
    _get_group_state,
)
def _build_request(group_id: int, queue: list[QueuedMessage]):
    payload_messages = [item.payload for item in queue]
    return build_agent_request(
        conversation_id=str(group_id),
        platform="onebot.v11",
        messages=payload_messages,
        message_format=PLUGIN_CONFIG.message_format,
        debug=PLUGIN_CONFIG.debug,
    )


def _mention_reference_id(queue: list[QueuedMessage]) -> int | None:
    for item in reversed(queue):
        if item.mentioned_bot:
            message_id = item.message_id
            return message_id if isinstance(message_id, int) else None
    return None


def _idle_reply_probability(minute_index: int) -> float:
    target_minutes = max(
        1.0,
        float(PLUGIN_CONFIG.timing.idle_expect_minutes - PLUGIN_CONFIG.timing.idle_start_minutes),
    )
    shape = 2.0
    scale = target_minutes / math.gamma(1.0 + 1.0 / shape)
    start = max(0.0, float(minute_index - 1))
    end = float(minute_index)
    prob = 1.0 - math.exp(-(((end / scale) ** shape) - ((start / scale) ** shape)))
    return min(1.0, max(0.0, prob))


async def _try_reply(
    group_id: int,
    expected_queue_version: int,
    *,
    trigger: str,
    require_mention: bool,
    use_mention_reference: bool,
) -> bool:
    state = _get_group_state(group_id)
    queue_snapshot: list[QueuedMessage] = []
    mention_reference: int | None = None
    bot_id = ""
    run_token: int | None = None
    while True:
        should_wait = False
        async with state.lock:
            if state.queue_version != expected_queue_version:
                if require_mention:
                    logger.debug(
                        "mention trigger adjusted expected_version group={} trigger={} old_expected={} new_expected={}",
                        group_id,
                        trigger,
                        expected_queue_version,
                        state.queue_version,
                    )
                    expected_queue_version = state.queue_version
                else:
                    logger.debug(
                        "skip reply trigger={} group={} reason=stale_event expected_version={} actual_version={}",
                        trigger,
                        group_id,
                        expected_queue_version,
                        state.queue_version,
                    )
                    return False
            if not state.queue:
                logger.debug("skip reply trigger={} group={} reason=empty_queue", trigger, group_id)
                return False
            if state.replying:
                if require_mention:
                    should_wait = True
                else:
                    logger.debug("skip reply trigger={} group={} reason=already_replying", trigger, group_id)
                    return False
            else:
                mention_reference = _mention_reference_id(state.queue)
                if require_mention and mention_reference is None:
                    logger.info(
                        "skip reply trigger={} group={} reason=no_mention_in_queue queue_size={}",
                        trigger,
                        group_id,
                        len(state.queue),
                    )
                    return False
                queue_snapshot = list(state.queue)
                bot_id = state.bot_id
                state.queue.clear()
                run_token = _begin_reply_run(state)
                break
        if should_wait:
            logger.info(
                "wait reply trigger={} group={} reason=already_replying require_mention=true",
                trigger,
                group_id,
            )
            await asyncio.sleep(0.5)
    logger.info(
        "reply trigger={} group={} queue_size={} require_mention={} dequeued=true",
        trigger,
        group_id,
        len(queue_snapshot),
        require_mention,
    )

    request = _build_request(group_id, queue_snapshot)
    sent_any = False
    cancelled = False
    delivered_output_ids: list[str] = []
    reply_handle = None
    try:
        response, reply_handle = await _get_group_agent(group_id).arun_reply_first(request)
        reply_raw = response.reply if isinstance(response.reply, str) else str(response.reply or "")
        outputs = sorted(
            list(response.outputs or []),
            key=lambda item: int(getattr(item, "order", 0)),
        )
        cancelled = bool(response.cancelled)
        logger.debug(
            "reply generated trigger={} group={} chars={} outputs={} cancelled={}",
            trigger,
            group_id,
            len(reply_raw),
            len(outputs),
            cancelled,
        )

        async with state.lock:
            if run_token is None or state.active_reply_token != run_token:
                logger.info(
                    "skip reply send trigger={} group={} reason=stale_run run_token={} active_run_token={}",
                    trigger,
                    group_id,
                    run_token,
                    state.active_reply_token,
                )
                return False

        try:
            bot = get_bot(bot_id)
        except Exception:
            logger.warning("bot {} is unavailable, skip reply for group {}", bot_id, group_id)
            return False

        if not outputs:
            logger.info(
                "reply cancelled trigger={} group={} queue_size={}",
                trigger,
                group_id,
                len(queue_snapshot),
            )
            return True

        for output_index, output in enumerate(outputs):
            message = Message()
            if (
                output_index == 0
                and use_mention_reference
                and mention_reference is not None
            ):
                message.append(MessageSegment.reply(mention_reference))
            reply_text = str(getattr(output, "content", "") or "").strip() or "..."
            message.append(MessageSegment.text(reply_text))

            try:
                await bot.send_group_msg(group_id=group_id, message=message)
            except Exception:
                logger.exception(
                    "send reply failed in group {} output_index={} route={}",
                    group_id,
                    output_index,
                    getattr(output, "route_id", ""),
                )
                if sent_any:
                    logger.warning(
                        "partial reply sent trigger={} group={} delivered_outputs={}",
                        trigger,
                        group_id,
                        len(delivered_output_ids),
                    )
                    return True
                return False

            sent_any = True
            delivered_output_ids.append(str(getattr(output, "event_id", "")))
            logger.info(
                "reply sent trigger={} group={} output_index={} route={} queue_size={}",
                trigger,
                group_id,
                output_index,
                getattr(output, "route_id", ""),
                len(queue_snapshot),
            )

        return True
    except asyncio.CancelledError:
        logger.info("cancel reply trigger={} group={} reason=reply_task_cancelled", trigger, group_id)
        return False
    except Exception:
        logger.exception("kisaragirin run failed in group {}", group_id)
        return False
    finally:
        should_finalize_delivery_ids = list(delivered_output_ids)
        async with state.lock:
            if run_token is None or state.active_reply_token != run_token:
                should_finalize_delivery_ids = []
        if reply_handle is not None:
            try:
                await asyncio.shield(
                    _get_group_agent(group_id).afinalize_reply_first(
                        reply_handle,
                        delivered_output_ids=should_finalize_delivery_ids,
                    )
                )
            except asyncio.CancelledError:
                logger.warning("wait step5 finalize cancelled in group {}", group_id)
            except Exception:
                logger.exception("step5 finalize failed in group {}", group_id)
        async with state.lock:
            if run_token is None or state.active_reply_token != run_token:
                logger.info(
                    "skip reply finalize trigger={} group={} reason=stale_run run_token={} active_run_token={} sent={}",
                    trigger,
                    group_id,
                    run_token,
                    state.active_reply_token,
                    sent_any,
                )
                return False
            if not sent_any and not cancelled and queue_snapshot:
                logger.info(
                    "reply not sent, requeue snapshot trigger={} group={} snapshot_size={} current_queue_size={}",
                    trigger,
                    group_id,
                    len(queue_snapshot),
                    len(state.queue),
                )
                state.queue.extend(queue_snapshot)
                state.queue.sort(key=lambda item: (item.created_at, item.sequence))
            state.active_reply_token = None
            state.replying = False
            if state.queue:
                _refresh_workers(group_id, state, state.queue_version)
            else:
                state.last_message_at = 0.0


async def _scheduler_worker(group_id: int) -> None:
    state = _get_group_state(group_id)
    observed_queue_version = -1
    next_idle_minute_index = 1
    try:
        while True:
            wait_seconds: float | None = None
            trigger: tuple[str, int, bool, bool] | None = None
            async with state.lock:
                if state.queue_version != observed_queue_version:
                    observed_queue_version = state.queue_version
                    next_idle_minute_index = 1
                state.scheduler_event.clear()
                if state.replying or not state.queue:
                    wait_seconds = None
                else:
                    now = time.time()
                    mention_reference = _mention_reference_id(state.queue)
                    quiet_seconds = max(0.0, float(PLUGIN_CONFIG.timing.mention_quiet_seconds))
                    if mention_reference is not None:
                        quiet_due_at = state.last_message_at + quiet_seconds
                        if now >= quiet_due_at:
                            logger.info(
                                "mention quiet timeout reached group={} quiet_seconds={}",
                                group_id,
                                PLUGIN_CONFIG.timing.mention_quiet_seconds,
                            )
                            trigger = (
                                "mention_quiet",
                                observed_queue_version,
                                True,
                                True,
                            )
                        else:
                            wait_seconds = max(0.0, quiet_due_at - now)
                    else:
                        idle_started_at = state.last_message_at + max(
                            0.0,
                            float(PLUGIN_CONFIG.timing.idle_start_minutes * 60),
                        )
                        next_draw_at = idle_started_at + max(0, next_idle_minute_index - 1) * 60
                        if now >= next_draw_at:
                            probability = _idle_reply_probability(next_idle_minute_index)
                            draw = random.random()
                            hit = draw < probability
                            logger.info(
                                "idle draw group={} minute={} probability={:.4f} draw={:.4f} hit={}",
                                group_id,
                                next_idle_minute_index,
                                probability,
                                draw,
                                hit,
                            )
                            if hit:
                                trigger = (
                                    f"idle_random_m{next_idle_minute_index}",
                                    observed_queue_version,
                                    False,
                                    False,
                                )
                            next_idle_minute_index += 1
                            if trigger is None:
                                next_draw_at = idle_started_at + max(
                                    0,
                                    next_idle_minute_index - 1,
                                ) * 60
                                wait_seconds = max(0.0, next_draw_at - time.time())
                        else:
                            wait_seconds = max(0.0, next_draw_at - now)
            if trigger is not None:
                replied = await _try_reply(
                    group_id,
                    trigger[1],
                    trigger=trigger[0],
                    require_mention=trigger[2],
                    use_mention_reference=trigger[3],
                )
                if replied:
                    continue
                continue
            if wait_seconds is None:
                await state.scheduler_event.wait()
                continue
            try:
                await asyncio.wait_for(state.scheduler_event.wait(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                continue
    except asyncio.CancelledError:
        return


def _refresh_workers(group_id: int, state: GroupState, expected_queue_version: int) -> None:
    if state.scheduler_task is None or state.scheduler_task.done():
        state.scheduler_task = asyncio.create_task(
            _scheduler_worker(group_id),
            name=f"kisaragirin-scheduler-{group_id}",
        )
    state.scheduler_event.set()
    logger.debug(
        "scheduler refreshed group={} queue_version={} replying={} quiet_seconds={} idle_start_minutes={}",
        group_id,
        expected_queue_version,
        state.replying,
        PLUGIN_CONFIG.timing.mention_quiet_seconds,
        PLUGIN_CONFIG.timing.idle_start_minutes,
    )
