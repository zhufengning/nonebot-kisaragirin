from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent

from .config import PLUGIN_CONFIG
from .state import _clear_group_queue, _get_group_agent

OPS_SET = {int(user_id) for user_id in PLUGIN_CONFIG.ops}
COMMAND_PATTERN = re.compile(
    r"^/(clear|clears|clearl|help|ov_init_commit|clear_empty_cache)(?:\s+.*)?$",
    re.IGNORECASE,
)
COMMAND_HELP_TEXT = (
    "可用指令：\n"
    "/help - 查看指令帮助\n"
    "/clear - 清空当前群消息队列 + 清除短期/长期记忆\n"
    "/clears - 只清除短期记忆\n"
    "/clearl - 只清除长期记忆\n"
    "/ov_init_commit - 将当前群已有长期记忆手动提交一次到 OpenViking\n"
    "/clear_empty_cache - 清理 URL/图片缓存中内容为空的条目"
)


def _is_ops_user(user_id: int) -> bool:
    return int(user_id) in OPS_SET


def _match_command(text: str) -> str | None:
    match = COMMAND_PATTERN.match(text.strip())
    if not match:
        return None
    return match.group(1).lower()


async def handle_ops_command_event(
    event: MessageEvent,
    finish: Callable[..., Awaitable[Any]],
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await finish()
        return
    group_id = event.group_id
    if group_id not in PLUGIN_CONFIG.groups:
        await finish()
        return

    if not _is_ops_user(event.user_id):
        await finish("Access Denied")
        return

    command = _match_command(event.get_plaintext())
    if command is None:
        await finish()
        return

    agent = _get_group_agent(group_id)

    if command == "help":
        await finish(COMMAND_HELP_TEXT)
        return
    if command == "clears":
        await asyncio.to_thread(agent.clear_short_term_memory, str(group_id))
        await finish("已清除本群短期记忆。")
        return
    if command == "clearl":
        await asyncio.to_thread(agent.clear_long_term_memory, str(group_id))
        await finish("已清除本群长期记忆。")
        return
    if command == "ov_init_commit":
        status = await asyncio.to_thread(
            agent.init_commit_openviking_long_term_memory,
            str(group_id),
        )
        if status == "empty":
            await finish("本群当前没有可初始化提交的长期记忆。")
            return
        await finish(f"已执行 OpenViking 初始化提交，status={status}")
        return
    if command == "clear_empty_cache":
        result = await asyncio.to_thread(agent.clear_empty_cache_entries)
        await finish(
            "已清理空缓存项："
            f"url_summary_cache={int(result.get('url_summaries', 0) or 0)}，"
            f"image_description_cache={int(result.get('image_descriptions', 0) or 0)}"
        )
        return

    await _clear_group_queue(group_id)
    await asyncio.to_thread(agent.clear_conversation, str(group_id))
    await finish("已清空本群会话与消息队列。")
