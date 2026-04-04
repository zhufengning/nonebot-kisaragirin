from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from itertools import count
from typing import Any

from kisaragirin import AgentConfig, KisaragiAgent, PromptConfig

from .config import PLUGIN_CONFIG
from .payload import MessageData


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
    scheduler_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_message_at: float = 0.0
    queue_version: int = 0
    bot_id: str = ""
    bot_name: str = ""
    replying: bool = False
    reply_token_counter: int = 0
    active_reply_token: int | None = None
    scheduler_task: asyncio.Task[None] | None = None


_QUEUE_SEQUENCE = count(1)
_GROUP_STATES: dict[int, GroupState] = {}
_GROUP_AGENTS: dict[int, KisaragiAgent] = {}


def next_queue_sequence() -> int:
    return next(_QUEUE_SEQUENCE)


def _get_group_state(group_id: int) -> GroupState:
    state = _GROUP_STATES.get(group_id)
    if state is None:
        state = GroupState()
        _GROUP_STATES[group_id] = state
    return state


def _get_group_agent(group_id: int) -> KisaragiAgent:
    state = _get_group_state(group_id)
    agent = _GROUP_AGENTS.get(group_id)
    if agent is not None:
        agent.set_self_name(state.bot_name or "assistant")
        return agent
    group_config = PLUGIN_CONFIG.groups[group_id]
    crawler_config = getattr(PLUGIN_CONFIG, "crawler", None)
    agent_kwargs: dict[str, Any] = {
        "message_format": PLUGIN_CONFIG.message_format,
        "self_name": state.bot_name or "assistant",
        "exa_api_key": PLUGIN_CONFIG.exa_api_key,
        "brave_search_api_key": PLUGIN_CONFIG.brave_search_api_key,
        "serpapi_api_key": PLUGIN_CONFIG.serpapi_api_key,
        "memory_db_path": PLUGIN_CONFIG.memory_db_path,
        "short_term_turn_window": PLUGIN_CONFIG.short_term_turn_window,
    }
    if crawler_config is not None:
        agent_kwargs["crawler"] = crawler_config

    agent_config = AgentConfig.from_model_list(
        models=list(PLUGIN_CONFIG.models),
        step_models=PLUGIN_CONFIG.step_models,
        prompts=PromptConfig(
            persona=group_config.persona,
            fixed_memory=group_config.fixed_memory,
        ),
        **agent_kwargs,
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


def _begin_reply_run(state: GroupState) -> int:
    state.reply_token_counter += 1
    state.active_reply_token = state.reply_token_counter
    state.replying = True
    return state.active_reply_token


def _invalidate_reply_run(state: GroupState) -> None:
    state.reply_token_counter += 1
    state.active_reply_token = None
    state.replying = False


async def _clear_group_queue(group_id: int) -> None:
    state = _get_group_state(group_id)
    async with state.lock:
        state.queue.clear()
        state.last_message_at = 0.0
        state.queue_version += 1
        _invalidate_reply_run(state)
        state.scheduler_event.set()


async def shutdown_plugin() -> None:
    for state in _GROUP_STATES.values():
        _cancel_task(state.scheduler_task)
    agents = list(_GROUP_AGENTS.values())
    _GROUP_AGENTS.clear()
    for agent in agents:
        await asyncio.to_thread(agent.close)
