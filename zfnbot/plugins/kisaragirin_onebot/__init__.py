from __future__ import annotations

import asyncio
import base64
import math
import mimetypes
import random
import re
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any
from urllib.parse import unquote_to_bytes

import httpx
from kisaragirin import AgentConfig, ImageInput, KisaragiAgent, PromptConfig
from nonebot import get_bot, get_driver, logger, on_message, on_regex
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.plugin import PluginMetadata

from .config import PLUGIN_CONFIG
from .payload import MessageData, MessageSegmentData, build_agent_request

__plugin_meta__ = PluginMetadata(
    name="kisaragirin_onebot",
    description="Queue-based onebot adapter for kisaragirin",
    usage="/help | /clear | /clears | /clearl",
)


@dataclass(slots=True)
class QueuedMessage:
    created_at: float
    sequence: int
    message_id: int | str
    mentioned_bot: bool
    payload: MessageData


@dataclass(slots=True)
class GroupState:
    queue: list[QueuedMessage] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_message_at: float = 0.0
    queue_version: int = 0
    bot_id: str = ""
    replying: bool = False
    flush_task: asyncio.Task[None] | None = None
    idle_task: asyncio.Task[None] | None = None


_QUEUE_SEQUENCE = count(1)
_GROUP_STATES: dict[int, GroupState] = {}
_GROUP_AGENTS: dict[int, KisaragiAgent] = {}

on_all_msg = on_message(priority=20, block=False)
on_ops_cmd = on_regex(
    r"^/(clear|clears|clearl|help)(?:\s+.*)?$",
    flags=re.IGNORECASE,
    priority=5,
    block=True,
)

logger.info("kisaragirin_onebot enabled groups: {}", sorted(PLUGIN_CONFIG.groups.keys()))

AT_TEXT_PATTERNS = (
    re.compile(r"\[at:qq=(\d+)\]"),
    re.compile(r"\[CQ:at,qq=(\d+)\]"),
)
MAX_REPLY_DEPTH = 4
OPS_SET = {int(user_id) for user_id in PLUGIN_CONFIG.ops}
COMMAND_PATTERN = re.compile(r"^/(clear|clears|clearl|help)(?:\s+.*)?$", re.IGNORECASE)
COMMAND_HELP_TEXT = (
    "可用指令：\n"
    "/help - 查看指令帮助\n"
    "/clear - 清空当前群消息队列 + 清除短期/长期记忆\n"
    "/clears - 只清除短期记忆\n"
    "/clearl - 只清除长期记忆"
)


def _get_group_state(group_id: int) -> GroupState:
    state = _GROUP_STATES.get(group_id)
    if state is None:
        state = GroupState()
        _GROUP_STATES[group_id] = state
    return state


def _get_group_agent(group_id: int) -> KisaragiAgent:
    agent = _GROUP_AGENTS.get(group_id)
    if agent is not None:
        return agent
    group_config = PLUGIN_CONFIG.groups[group_id]
    agent_config = AgentConfig.from_model_list(
        models=list(PLUGIN_CONFIG.models),
        step_models=PLUGIN_CONFIG.step_models,
        prompts=PromptConfig(persona=group_config.persona, fixed_memory=group_config.fixed_memory),
        exa_api_key=PLUGIN_CONFIG.exa_api_key,
        brave_search_api_key=PLUGIN_CONFIG.brave_search_api_key,
        serpapi_api_key=PLUGIN_CONFIG.serpapi_api_key,
        memory_db_path=PLUGIN_CONFIG.memory_db_path,
    )
    agent = KisaragiAgent(agent_config)
    _GROUP_AGENTS[group_id] = agent
    return agent


def _is_ops_user(user_id: int) -> bool:
    return int(user_id) in OPS_SET


def _match_command(text: str) -> str | None:
    match = COMMAND_PATTERN.match(text.strip())
    if not match:
        return None
    return match.group(1).lower()


def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    if task is asyncio.current_task():
        return
    task.cancel()


async def _clear_group_queue(group_id: int) -> None:
    state = _get_group_state(group_id)
    async with state.lock:
        state.queue.clear()
        state.last_message_at = 0.0
        state.queue_version += 1
        state.replying = False
        _cancel_task(state.flush_task)
        _cancel_task(state.idle_task)


def _sender_name(event: GroupMessageEvent) -> str:
    sender = event.sender
    if sender:
        card = str(getattr(sender, "card", "") or "").strip()
        if card:
            return card
        nickname = str(getattr(sender, "nickname", "") or "").strip()
        if nickname:
            return nickname
    return str(event.user_id)


def _sender_name_from_dict(sender: dict[str, Any], user_id: int) -> str:
    card = str(sender.get("card") or "").strip()
    if card:
        return card
    nickname = str(sender.get("nickname") or "").strip()
    if nickname:
        return nickname
    return str(user_id)


def _image_input_from_data_uri(uri: str, image_name: str | None) -> MessageSegmentData | None:
    if not uri.startswith("data:"):
        return None
    if "," not in uri:
        return None
    header, data = uri.split(",", 1)
    mime_type = header[5:].split(";", 1)[0].strip() or "image/png"
    if ";base64" in header:
        base64_data = data
    else:
        base64_data = base64.b64encode(unquote_to_bytes(data)).decode("ascii")
    return MessageSegmentData(
        type="image",
        image_name=image_name,
        image=ImageInput(
            base64_data=base64_data,
            mime_type=mime_type,
            name=image_name,
        ),
    )


async def _image_segment_to_data(bot: Bot, segment: MessageSegment) -> MessageSegmentData | None:
    file_ref = str(segment.data.get("file") or "").strip()
    image_url = str(segment.data.get("url") or "").strip()
    image_name = file_ref or None

    data_uri_segment = _image_input_from_data_uri(file_ref, image_name)
    if data_uri_segment is not None:
        return data_uri_segment

    data_uri_segment = _image_input_from_data_uri(image_url, image_name)
    if data_uri_segment is not None:
        return data_uri_segment

    content: bytes | None = None
    mime_type: str | None = None

    if False and file_ref:
        try:
            image_info = await bot.get_image(file=file_ref)
            if isinstance(image_info, dict):
                image_file = image_info.get("file")
                if isinstance(image_file, str) and image_file:
                    file_path = Path(image_file)
                    if file_path.is_file():
                        content = file_path.read_bytes()
                        image_name = image_name or file_path.name
                        guessed = mimetypes.guess_type(file_path.name)[0]
                        if guessed and guessed.startswith("image/"):
                            mime_type = guessed
                if content is None:
                    fetched_url = image_info.get("url")
                    if isinstance(fetched_url, str) and fetched_url:
                        image_url = fetched_url
        except Exception:
            logger.opt(exception=True).debug("onebot get_image failed: file={}", file_ref)

    if content is None and image_url:
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(image_url)
                response.raise_for_status()
                content = response.content
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type.startswith("image/"):
                    mime_type = content_type
        except Exception:
            logger.warning("download image failed, skip one segment")
            return None

    if not content:
        return None

    if not mime_type:
        guessed = mimetypes.guess_type(image_name or "")[0]
        mime_type = guessed if guessed and guessed.startswith("image/") else "image/png"

    return MessageSegmentData(
        type="image",
        image_name=image_name,
        image=ImageInput(
            base64_data=base64.b64encode(content).decode("ascii"),
            mime_type=mime_type,
            name=image_name,
        ),
    )


def _normalize_message_id(raw_message_id: Any) -> int | str | None:
    if raw_message_id is None:
        return None
    if isinstance(raw_message_id, int):
        return raw_message_id
    text = str(raw_message_id).strip()
    if not text:
        return None
    return int(text) if text.isdigit() else text


def _coerce_to_message(raw_message: Any) -> Message:
    if isinstance(raw_message, Message):
        return raw_message
    if isinstance(raw_message, str):
        return Message(raw_message)

    normalized: list[Any]
    if isinstance(raw_message, dict):
        normalized = [raw_message]
    elif isinstance(raw_message, (list, tuple)):
        normalized = list(raw_message)
    else:
        return Message(str(raw_message or ""))

    message = Message()
    for item in normalized:
        if isinstance(item, MessageSegment):
            message.append(item)
            continue
        if isinstance(item, dict):
            segment_type = str(item.get("type") or "").strip()
            segment_data = item.get("data")
            if segment_type and isinstance(segment_data, dict):
                message.append(MessageSegment(segment_type, segment_data))
                continue
        if isinstance(item, str) and item:
            message.append(MessageSegment.text(item))

    if not message and raw_message is not None:
        return Message(str(raw_message))
    return message


async def _load_reply_message(
    bot: Bot,
    *,
    group_id: int,
    reply_message_id: str,
    bot_id: str,
    depth: int,
    seen: set[str],
) -> MessageData | None:
    if depth > MAX_REPLY_DEPTH:
        logger.debug(
            "reply chain exceeded max depth group={} reply_message_id={} depth={}",
            group_id,
            reply_message_id,
            depth,
        )
        return None
    if reply_message_id in seen:
        logger.debug(
            "reply chain loop detected group={} reply_message_id={} depth={}",
            group_id,
            reply_message_id,
            depth,
        )
        return None

    normalized_message_id = _normalize_message_id(reply_message_id)
    if normalized_message_id is None:
        return None

    seen.add(reply_message_id)
    try:
        response = await bot.get_msg(message_id=normalized_message_id)
    except Exception:
        logger.warning(
            "get_msg failed group={} reply_message_id={} depth={}",
            group_id,
            reply_message_id,
            depth,
        )
        return None

    if not isinstance(response, dict):
        return None
    message_id = response.get("message_id", reply_message_id)
    sender = response.get("sender")
    sender_info = sender if isinstance(sender, dict) else {}
    user_id_raw = response.get("user_id", sender_info.get("user_id", 0))
    user_id = int(user_id_raw) if str(user_id_raw).isdigit() else 0
    created_at_raw = response.get("time", 0.0)
    try:
        created_at = float(created_at_raw)
    except Exception:
        created_at = 0.0

    raw_message = response.get("message")
    message_obj = _coerce_to_message(raw_message)
    segments, _, has_unknown_segment = await _parse_segments(
        bot,
        message=message_obj,
        bot_id=bot_id,
        group_id=group_id,
        message_id=message_id,
        mentioned_bot=False,
        detect_mention=False,
        depth=depth,
        seen=seen,
    )
    return MessageData(
        message_id=message_id,
        created_at=created_at,
        sender_id=user_id,
        sender_name=_sender_name_from_dict(sender_info, user_id),
        mentioned_bot=False,
        segments=segments,
        has_unknown_segment=has_unknown_segment,
    )


async def _parse_segments(
    bot: Bot,
    *,
    message: Message,
    bot_id: str,
    group_id: int,
    message_id: int | str,
    mentioned_bot: bool,
    detect_mention: bool,
    depth: int,
    seen: set[str],
) -> tuple[list[MessageSegmentData], bool, bool]:
    segments: list[MessageSegmentData] = []
    has_unknown_segment = False
    for idx, segment in enumerate(message, start=1):
        segment_type = segment.type
        segment_data = dict(segment.data)
        logger.debug(
            "segment detected group={} message_id={} index={} depth={} type={} data={}",
            group_id,
            message_id,
            idx,
            depth,
            segment_type,
            segment_data,
        )

        if detect_mention:
            qq_value = segment_data.get("qq")
            if qq_value is not None:
                at_target = str(qq_value).strip()
                if at_target == bot_id:
                    mentioned_bot = True
                    logger.info(
                        "bot mention matched group={} message_id={} index={} target={}",
                        group_id,
                        message_id,
                        idx,
                        at_target,
                    )

        if segment_type == "reply":
            reply_raw_id = segment_data.get("id", segment_data.get("message_id"))
            reply_message_id = str(reply_raw_id or "").strip()
            if not reply_message_id:
                continue
            reply_message = await _load_reply_message(
                bot,
                group_id=group_id,
                reply_message_id=reply_message_id,
                bot_id=bot_id,
                depth=depth + 1,
                seen=seen,
            )
            segments.append(
                MessageSegmentData(
                    type="reply",
                    reply_message_id=reply_message_id,
                    reply=reply_message,
                )
            )
            continue

        if segment_type in {"at", "mention"}:
            continue

        if segment_type == "text":
            text = str(segment_data.get("text", ""))
            if text:
                if detect_mention and not mentioned_bot:
                    for pattern in AT_TEXT_PATTERNS:
                        match = pattern.search(text)
                        if match and match.group(1) == bot_id:
                            mentioned_bot = True
                            logger.info(
                                "bot mention matched from text group={} message_id={} index={}",
                                group_id,
                                message_id,
                                idx,
                            )
                            break
                segments.append(MessageSegmentData(type="text", text=text))
            continue

        if segment_type == "image":
            image_segment = await _image_segment_to_data(bot, segment)
            if image_segment is not None:
                segments.append(image_segment)
            continue

        has_unknown_segment = True
        logger.debug(
            "unknown segment kept only in merged_text group={} message_id={} index={} depth={} type={}",
            group_id,
            message_id,
            idx,
            depth,
            segment_type,
        )

    return segments, mentioned_bot, has_unknown_segment


async def _parse_message(
    bot: Bot,
    event: GroupMessageEvent,
    bot_id: str,
) -> tuple[list[MessageSegmentData], bool, bool]:
    mentioned_bot = bool(event.is_tome())
    logger.debug(
        "message meta group={} message_id={} to_me={} raw_message={}",
        event.group_id,
        event.message_id,
        mentioned_bot,
        getattr(event, "raw_message", ""),
    )
    if mentioned_bot:
        logger.info(
            "bot mention inferred by to_me group={} message_id={}",
            event.group_id,
            event.message_id,
        )

    event_reply = getattr(event, "reply", None)
    prefixed_reply_segment: MessageSegmentData | None = None
    if event_reply is not None:
        logger.debug(
            "event.reply detected group={} message_id={} reply={}",
            event.group_id,
            event.message_id,
            event_reply,
        )
        event_reply_id = str(getattr(event_reply, "message_id", "") or "").strip()
        if event_reply_id:
            nested_reply = await _load_reply_message(
                bot,
                group_id=event.group_id,
                reply_message_id=event_reply_id,
                bot_id=bot_id,
                depth=1,
                seen=set(),
            )
            if nested_reply is None:
                reply_message_obj = getattr(event_reply, "message", Message())
                reply_sender = getattr(event_reply, "sender", None)
                reply_sender_dict = (
                    reply_sender.model_dump() if hasattr(reply_sender, "model_dump") else {}
                )
                reply_user_id_raw = reply_sender_dict.get("user_id", 0)
                reply_user_id = int(reply_user_id_raw) if str(reply_user_id_raw).isdigit() else 0
                fallback_segments, _, fallback_has_unknown = await _parse_segments(
                    bot,
                    message=_coerce_to_message(reply_message_obj),
                    bot_id=bot_id,
                    group_id=event.group_id,
                    message_id=event_reply_id,
                    mentioned_bot=False,
                    detect_mention=False,
                    depth=1,
                    seen={event_reply_id},
                )
                nested_reply = MessageData(
                    message_id=event_reply_id,
                    created_at=float(getattr(event_reply, "time", 0)),
                    sender_id=reply_user_id,
                    sender_name=_sender_name_from_dict(reply_sender_dict, reply_user_id),
                    mentioned_bot=False,
                    segments=fallback_segments,
                    has_unknown_segment=fallback_has_unknown,
                )
            prefixed_reply_segment = MessageSegmentData(
                type="reply",
                reply_message_id=event_reply_id,
                reply=nested_reply,
            )

    parsed_segments, parsed_mentioned_bot, parsed_has_unknown = await _parse_segments(
        bot,
        message=event.message,
        bot_id=bot_id,
        group_id=event.group_id,
        message_id=event.message_id,
        mentioned_bot=mentioned_bot,
        detect_mention=True,
        depth=0,
        seen=set(),
    )
    if prefixed_reply_segment is not None and not any(
        segment.type == "reply" for segment in parsed_segments
    ):
        parsed_segments.insert(0, prefixed_reply_segment)
    return parsed_segments, parsed_mentioned_bot, parsed_has_unknown


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
    try:
        response, finalize_future = await _get_group_agent(group_id).arun_reply_first(request)
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


@on_all_msg.handle()
async def handle_group_message(bot: Bot, event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        return
    group_id = event.group_id
    if group_id not in PLUGIN_CONFIG.groups:
        return
    if _match_command(event.get_plaintext()) is not None:
        return

    runtime_bot_id = str(bot.self_id)
    segments, mentioned_bot, has_unknown_segment = await _parse_message(bot, event, runtime_bot_id)
    if not segments and not mentioned_bot:
        return
    if mentioned_bot:
        logger.info(
            "bot mentioned group={} message_id={} user_id={}",
            group_id,
            event.message_id,
            event.user_id,
        )

    created_at = float(event.time)
    queued = QueuedMessage(
        created_at=created_at,
        sequence=next(_QUEUE_SEQUENCE),
        message_id=event.message_id,
        mentioned_bot=mentioned_bot,
        payload=MessageData(
            message_id=event.message_id,
            created_at=created_at,
            sender_id=event.user_id,
            sender_name=_sender_name(event),
            mentioned_bot=mentioned_bot,
            segments=segments,
            has_unknown_segment=has_unknown_segment,
        ),
    )
    state = _get_group_state(group_id)
    async with state.lock:
        current_bot_id = runtime_bot_id
        if state.bot_id != current_bot_id:
            logger.info(
                "bot id initialized group={} bot_id={} previous_bot_id={}",
                group_id,
                current_bot_id,
                state.bot_id or "(none)",
            )
        state.bot_id = current_bot_id
        state.last_message_at = queued.created_at
        state.queue_version += 1
        state.queue.append(queued)
        state.queue.sort(key=lambda item: (item.created_at, item.sequence))
        logger.debug(
            "message queued group={} message_id={} queue_size={} mentioned_bot={} queue_version={}",
            group_id,
            event.message_id,
            len(state.queue),
            mentioned_bot,
            state.queue_version,
        )
        _refresh_workers(group_id, state, state.queue_version)


@on_ops_cmd.handle()
async def handle_ops_command(event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        await on_ops_cmd.finish()
        return
    group_id = event.group_id
    if group_id not in PLUGIN_CONFIG.groups:
        await on_ops_cmd.finish()
        return

    if not _is_ops_user(event.user_id):
        await on_ops_cmd.finish("Access Denied")
        return

    command = _match_command(event.get_plaintext())
    if command is None:
        await on_ops_cmd.finish()
        return

    agent = _get_group_agent(group_id)

    if command == "help":
        await on_ops_cmd.finish(COMMAND_HELP_TEXT)
        return
    if command == "clears":
        await asyncio.to_thread(agent.clear_short_term_memory, str(group_id))
        await on_ops_cmd.finish("已清除本群短期记忆。")
        return
    if command == "clearl":
        await asyncio.to_thread(agent.clear_long_term_memory, str(group_id))
        await on_ops_cmd.finish("已清除本群长期记忆。")
        return

    await _clear_group_queue(group_id)
    await asyncio.to_thread(agent.clear_conversation, str(group_id))
    await on_ops_cmd.finish("已清空本群会话与消息队列。")


driver = get_driver()


@driver.on_shutdown
async def _shutdown_plugin() -> None:
    for state in _GROUP_STATES.values():
        _cancel_task(state.flush_task)
        _cancel_task(state.idle_task)
    agents = list(_GROUP_AGENTS.values())
    _GROUP_AGENTS.clear()
    for agent in agents:
        await asyncio.to_thread(agent.close)
