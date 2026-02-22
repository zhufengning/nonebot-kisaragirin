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
    usage="/clear",
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
    bot_id: str = ""
    replying: bool = False
    flush_task: asyncio.Task[None] | None = None
    idle_task: asyncio.Task[None] | None = None


_QUEUE_SEQUENCE = count(1)
_GROUP_STATES: dict[int, GroupState] = {}
_GROUP_AGENTS: dict[int, KisaragiAgent] = {}

on_all_msg = on_message(priority=20, block=False)
on_clear_cmd = on_regex(r"^/clear(?:\s+.*)?$", priority=5, block=True)

logger.info("kisaragirin_onebot enabled groups: {}", sorted(PLUGIN_CONFIG.groups.keys()))

AT_TEXT_PATTERNS = (
    re.compile(r"\[at:qq=(\d+)\]"),
    re.compile(r"\[CQ:at,qq=(\d+)\]"),
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
        prompts=PromptConfig(persona=group_config.persona),
        brave_search_api_key=PLUGIN_CONFIG.brave_search_api_key,
        serpapi_api_key=PLUGIN_CONFIG.serpapi_api_key,
        memory_db_path=PLUGIN_CONFIG.memory_db_path,
    )
    agent = KisaragiAgent(agent_config)
    _GROUP_AGENTS[group_id] = agent
    return agent


def _cancel_task(task: asyncio.Task[None] | None) -> None:
    if task is None or task.done():
        return
    if task is asyncio.current_task():
        return
    task.cancel()


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

    if file_ref:
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


async def _parse_message(
    bot: Bot,
    event: GroupMessageEvent,
    bot_id: str,
) -> tuple[list[MessageSegmentData], bool]:
    segments: list[MessageSegmentData] = []
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

    for idx, segment in enumerate(event.message, start=1):
        segment_type = segment.type
        segment_data = dict(segment.data)
        logger.debug(
            "segment detected group={} message_id={} index={} type={} data={}",
            event.group_id,
            event.message_id,
            idx,
            segment_type,
            segment_data,
        )

        qq_value = segment_data.get("qq")
        if qq_value is not None:
            at_target = str(qq_value).strip()
            if at_target == bot_id:
                mentioned_bot = True
                logger.info(
                    "bot mention matched group={} message_id={} index={} target={}",
                    event.group_id,
                    event.message_id,
                    idx,
                    at_target,
                )

        if segment_type in {"at", "mention"}:
            continue
        if segment_type == "text":
            text = str(segment_data.get("text", ""))
            if text:
                if not mentioned_bot:
                    for pattern in AT_TEXT_PATTERNS:
                        match = pattern.search(text)
                        if match and match.group(1) == bot_id:
                            mentioned_bot = True
                            logger.info(
                                "bot mention matched from text group={} message_id={} index={}",
                                event.group_id,
                                event.message_id,
                                idx,
                            )
                            break
                segments.append(MessageSegmentData(type="text", text=text))
            continue
        if segment_type == "image":
            image_segment = await _image_segment_to_data(bot, segment)
            if image_segment is not None:
                segments.append(image_segment)
    return segments, mentioned_bot


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
    expected_last_message_at: float,
    *,
    trigger: str,
    require_mention: bool,
    use_mention_reference: bool,
) -> bool:
    state = _get_group_state(group_id)
    async with state.lock:
        if state.last_message_at != expected_last_message_at:
            logger.debug(
                "skip reply trigger={} group={} reason=stale_event expected={} actual={}",
                trigger,
                group_id,
                expected_last_message_at,
                state.last_message_at,
            )
            return False
        if not state.queue:
            logger.debug("skip reply trigger={} group={} reason=empty_queue", trigger, group_id)
            return False
        if state.replying:
            logger.debug("skip reply trigger={} group={} reason=already_replying", trigger, group_id)
            return False
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
        state.replying = True
    logger.info(
        "reply trigger={} group={} queue_size={} require_mention={}",
        trigger,
        group_id,
        len(queue_snapshot),
        require_mention,
    )

    request = _build_request(group_id, queue_snapshot)
    try:
        response = await _get_group_agent(group_id).arun(request)
    except Exception:
        logger.exception("kisaragirin run failed in group {}", group_id)
        async with state.lock:
            state.replying = False
        return False

    try:
        bot = get_bot(bot_id)
    except Exception:
        logger.warning("bot {} is unavailable, skip reply for group {}", bot_id, group_id)
        async with state.lock:
            state.replying = False
        return False

    message = Message()
    if use_mention_reference and mention_reference is not None:
        message.append(MessageSegment.reply(mention_reference))
    reply_text = response.reply.strip() or "..."
    message.append(MessageSegment.text(reply_text))

    try:
        await bot.send_group_msg(group_id=group_id, message=message)
    except Exception:
        logger.exception("send reply failed in group {}", group_id)
        async with state.lock:
            state.replying = False
        return False
    logger.info(
        "reply sent trigger={} group={} queue_size={}",
        trigger,
        group_id,
        len(queue_snapshot),
    )

    async with state.lock:
        state.queue.clear()
        state.replying = False
        state.last_message_at = 0.0
        _cancel_task(state.flush_task)
        _cancel_task(state.idle_task)
    return True


async def _mention_quiet_worker(group_id: int, expected_last_message_at: float) -> None:
    try:
        await asyncio.sleep(max(0, PLUGIN_CONFIG.timing.mention_quiet_seconds))
        logger.info(
            "mention quiet timeout reached group={} quiet_seconds={}",
            group_id,
            PLUGIN_CONFIG.timing.mention_quiet_seconds,
        )
        await _try_reply(
            group_id,
            expected_last_message_at,
            trigger="mention_quiet",
            require_mention=True,
            use_mention_reference=True,
        )
    except asyncio.CancelledError:
        return


async def _idle_reply_worker(group_id: int, expected_last_message_at: float) -> None:
    start_wait_seconds = max(0, PLUGIN_CONFIG.timing.idle_start_minutes * 60)
    minute_index = 1
    try:
        await asyncio.sleep(start_wait_seconds)
        while True:
            state = _get_group_state(group_id)
            async with state.lock:
                if state.last_message_at != expected_last_message_at:
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
                    expected_last_message_at,
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


def _refresh_workers(group_id: int, state: GroupState, expected_last_message_at: float) -> None:
    _cancel_task(state.flush_task)
    _cancel_task(state.idle_task)
    state.flush_task = asyncio.create_task(
        _mention_quiet_worker(group_id, expected_last_message_at),
        name=f"kisaragirin-flush-{group_id}",
    )
    state.idle_task = asyncio.create_task(
        _idle_reply_worker(group_id, expected_last_message_at),
        name=f"kisaragirin-idle-{group_id}",
    )
    logger.debug(
        "workers refreshed group={} quiet_seconds={} idle_start_minutes={}",
        group_id,
        PLUGIN_CONFIG.timing.mention_quiet_seconds,
        PLUGIN_CONFIG.timing.idle_start_minutes,
    )


@on_all_msg.handle()
async def handle_group_message(bot: Bot, event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        return
    group_id = event.group_id
    if group_id not in PLUGIN_CONFIG.groups:
        logger.debug("group {} is not enabled, skip message", group_id)
        return
    if event.get_plaintext().strip().lower().startswith("/clear"):
        return

    runtime_bot_id = str(bot.self_id)
    segments, mentioned_bot = await _parse_message(bot, event, runtime_bot_id)
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
        state.queue.append(queued)
        state.queue.sort(key=lambda item: (item.created_at, item.sequence))
        logger.debug(
            "message queued group={} message_id={} queue_size={} mentioned_bot={}",
            group_id,
            event.message_id,
            len(state.queue),
            mentioned_bot,
        )
        _refresh_workers(group_id, state, state.last_message_at)


@on_clear_cmd.handle()
async def handle_clear_command(event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        await on_clear_cmd.finish()
        return
    group_id = event.group_id
    if group_id not in PLUGIN_CONFIG.groups:
        logger.debug("group {} is not enabled, skip /clear", group_id)
        await on_clear_cmd.finish()
        return

    state = _get_group_state(group_id)
    async with state.lock:
        state.queue.clear()
        state.last_message_at = 0.0
        state.replying = False
        _cancel_task(state.flush_task)
        _cancel_task(state.idle_task)

    agent = _get_group_agent(group_id)
    await asyncio.to_thread(agent.clear_conversation, str(group_id))
    await on_clear_cmd.finish("已清空本群会话与消息队列。")


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
