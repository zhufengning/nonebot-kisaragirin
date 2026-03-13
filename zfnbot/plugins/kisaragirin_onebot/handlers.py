from __future__ import annotations

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent

from .config import PLUGIN_CONFIG
from .ops import _match_command
from .parser import _parse_message, _sender_name
from .payload import MessageData
from .scheduler import _refresh_workers
from .state import QueuedMessage, _get_group_state, next_queue_sequence


async def handle_group_message_event(bot: Bot, event: MessageEvent) -> None:
    if not isinstance(event, GroupMessageEvent):
        return
    group_id = event.group_id
    if group_id not in PLUGIN_CONFIG.groups:
        return
    if _match_command(event.get_plaintext()) is not None:
        return

    runtime_bot_id = str(bot.self_id)
    segments, mentioned_bot, has_unknown_segment = await _parse_message(
        bot,
        event,
        runtime_bot_id,
    )
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
        sequence=next_queue_sequence(),
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
