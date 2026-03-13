from __future__ import annotations

import re

from nonebot import get_driver, logger, on_message, on_regex
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.plugin import PluginMetadata

from .config import PLUGIN_CONFIG
from .handlers import handle_group_message_event
from .ops import handle_ops_command_event
from .state import shutdown_plugin

__plugin_meta__ = PluginMetadata(
    name="kisaragirin_onebot",
    description="Queue-based onebot adapter for kisaragirin",
    usage="/help | /clear | /clears | /clearl",
)

on_all_msg = on_message(priority=20, block=False)
on_ops_cmd = on_regex(
    r"^/(clear|clears|clearl|help)(?:\s+.*)?$",
    flags=re.IGNORECASE,
    priority=5,
    block=True,
)

driver = get_driver()
logger.info("kisaragirin_onebot enabled groups: {}", sorted(PLUGIN_CONFIG.groups.keys()))


@on_all_msg.handle()
async def handle_group_message(bot: Bot, event: MessageEvent) -> None:
    await handle_group_message_event(bot, event)


@on_ops_cmd.handle()
async def handle_ops_command(event: MessageEvent) -> None:
    await handle_ops_command_event(event, on_ops_cmd.finish)


@driver.on_shutdown
async def _shutdown_plugin() -> None:
    await shutdown_plugin()
