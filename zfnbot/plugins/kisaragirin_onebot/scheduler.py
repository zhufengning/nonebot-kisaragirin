from __future__ import annotations

import asyncio
import math
import random

from nonebot import get_bot, logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .config import PLUGIN_CONFIG
from .payload import build_agent_request
from .state import GroupState, QueuedMessage, _cancel_task, _get_group_agent, _get_group_state
def _build_request(group_id: int, queue: list[QueuedMessage]):
    payload_messages = [item.payload for item in queue]
    return build_agent_request(
        conversation_id=str(group_id),
        platform="onebot.v11",
        messages=payload_messages,
        debug=PLUGIN_CONFIG.debug,
    )


def _mention_reference_id(queue: list[QueuedMessage]) -> int | str | None:
    for item in reversed(queue):
        if item.mentioned_bot:
            return item.message_id
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
                state.replying = True
                _cancel_task(state.flush_task)
                _cancel_task(state.idle_task)
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
    if PLUGIN_CONFIG.debug:
        logger.debug(
            "reply request prepared trigger={} group={} queue_version={} message_ids={}",
            trigger,
            group_id,
            expected_queue_version,
            [str(item.message_id) for item in queue_snapshot],
        )

    sent = False
    finalize_future: asyncio.Future[None] | None = None
    delivery_future = None
    try:
        response, finalize_future, delivery_future = await _get_group_agent(group_id).arun_reply_first(request)
        reply_raw = response.reply if isinstance(response.reply, str) else str(response.reply or "")
        logger.debug(
            "reply generated trigger={} group={} chars={}",
            trigger,
            group_id,
            len(reply_raw),
        )

        try:
            bot = get_bot(bot_id)
        except Exception:
            logger.warning("bot {} is unavailable, skip reply for group {}", bot_id, group_id)
            return False

        message = Message()
        if use_mention_reference and mention_reference is not None:
            message.append(MessageSegment.reply(mention_reference))
        reply_text = reply_raw.strip() or "..."
        message.append(MessageSegment.text(reply_text))

        try:
            await bot.send_group_msg(group_id=group_id, message=message)
        except Exception:
            logger.exception("send reply failed in group {}", group_id)
            return False
        logger.info(
            "reply sent trigger={} group={} queue_size={}",
            trigger,
            group_id,
            len(queue_snapshot),
        )

        sent = True
        return True
    except asyncio.CancelledError:
        logger.info("cancel reply trigger={} group={} reason=reply_task_cancelled", trigger, group_id)
        return False
    except Exception:
        logger.exception("kisaragirin run failed in group {}", group_id)
        return False
    finally:
        if delivery_future is not None and not delivery_future.done():
            delivery_future.set_result(sent)
        if finalize_future is not None:
            try:
                await asyncio.shield(finalize_future)
            except asyncio.CancelledError:
                logger.warning("wait step5 finalize cancelled in group {}", group_id)
            except Exception:
                logger.exception("step5 finalize failed in group {}", group_id)
        async with state.lock:
            if not sent and queue_snapshot:
                logger.info(
                    "reply not sent, requeue snapshot trigger={} group={} snapshot_size={} current_queue_size={}",
                    trigger,
                    group_id,
                    len(queue_snapshot),
                    len(state.queue),
                )
                state.queue.extend(queue_snapshot)
                state.queue.sort(key=lambda item: (item.created_at, item.sequence))
            state.replying = False
            if state.queue:
                _refresh_workers(group_id, state, state.queue_version)
            else:
                state.last_message_at = 0.0


async def _mention_quiet_worker(group_id: int, expected_queue_version: int) -> None:
    try:
        await asyncio.sleep(max(0, PLUGIN_CONFIG.timing.mention_quiet_seconds))
        logger.info(
            "mention quiet timeout reached group={} quiet_seconds={}",
            group_id,
            PLUGIN_CONFIG.timing.mention_quiet_seconds,
        )
        await _try_reply(
            group_id,
            expected_queue_version,
            trigger="mention_quiet",
            require_mention=True,
            use_mention_reference=True,
        )
    except asyncio.CancelledError:
        return


async def _idle_reply_worker(group_id: int, expected_queue_version: int) -> None:
    start_wait_seconds = max(0, PLUGIN_CONFIG.timing.idle_start_minutes * 60)
    minute_index = 1
    try:
        await asyncio.sleep(start_wait_seconds)
        while True:
            state = _get_group_state(group_id)
            async with state.lock:
                if state.queue_version != expected_queue_version:
                    return
                if not state.queue:
                    return
            probability = _idle_reply_probability(minute_index)
            draw = random.random()
            hit = draw < probability
            logger.info(
                "idle draw group={} minute={} probability={:.4f} draw={:.4f} hit={}",
                group_id,
                minute_index,
                probability,
                draw,
                hit,
            )
            if hit:
                replied = await _try_reply(
                    group_id,
                    expected_queue_version,
                    trigger=f"idle_random_m{minute_index}",
                    require_mention=False,
                    use_mention_reference=False,
                )
                if replied:
                    return
            minute_index += 1
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        return


def _refresh_workers(group_id: int, state: GroupState, expected_queue_version: int) -> None:
    if state.replying:
        logger.debug(
            "workers refresh deferred group={} reason=reply_in_progress queue_version={}",
            group_id,
            expected_queue_version,
        )
        return
    _cancel_task(state.flush_task)
    _cancel_task(state.idle_task)
    state.flush_task = asyncio.create_task(
        _mention_quiet_worker(group_id, expected_queue_version),
        name=f"kisaragirin-flush-{group_id}",
    )
    state.idle_task = asyncio.create_task(
        _idle_reply_worker(group_id, expected_queue_version),
        name=f"kisaragirin-idle-{group_id}",
    )
    logger.debug(
        "workers refreshed group={} queue_version={} quiet_seconds={} idle_start_minutes={}",
        group_id,
        expected_queue_version,
        PLUGIN_CONFIG.timing.mention_quiet_seconds,
        PLUGIN_CONFIG.timing.idle_start_minutes,
    )


