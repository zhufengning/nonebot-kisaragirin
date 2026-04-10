from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from threading import Lock, Thread
from typing import Annotated, Any, TypedDict, cast
from urllib.parse import unquote_to_bytes, urlsplit

from crawl4ai import BrowserConfig
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
import yaml

from .config import (
    AgentConfig,
    ConversationRequest,
    ConversationResponse,
    ImageInput,
    OutputEvent,
)
from .memory import ShortTermMessage, SQLiteMemoryStore
from .orchestration import (
    StepImplementationRegistry,
    build_graph_for_execution_plan,
    resolve_all_steps,
)
from .prompts import (
    ANIMATED_VISION_DESCRIPTION_PROMPT,
    STEP_SYSTEM_INSTRUCTIONS,
    URL_SUMMARY_PROMPT_TEMPLATE,
    VISION_DESCRIPTION_PROMPT,
)
from .routing import (
    EMPTY_GRAPH,
    ExecutionPlan,
    RouteDecision,
    build_default_route_decision,
    build_execution_plan,
    build_route_selection_plan,
    normalize_route_ids,
)
from .steps_core import run_prepare
from .steps_enrichment import (
    run_enrich_merge,
    run_tools,
    run_urls,
    run_vision,
)
from .steps_response import (
    run_memory,
    run_memory_gate,
    run_reply,
    run_reply_lite_check,
    run_reply_lite,
)
from .steps_routing import run_route
from .tools import build_default_tools

# A practical URL matcher adapted from common "liberal" URL extraction patterns.
# Full RFC-style regexes are extremely long and often brittle in chat text.
URL_PATTERN = re.compile(
    r"""(?ix)
    \b(
      (?:
        https?://
        |ftp://
        |www\d{0,3}[.]
      )
      (?:
        [^\s()<>]+
        |
        \([^\s()<>]+\)
      )+
      (?:
        \([^\s()<>]+\)
        |
        [^\s`!()\[\]{};:'".,<>?«»“”‘’]
      )
    )
    """
)
LEGACY_IMAGE_SHA256_PATTERN = re.compile(r"\[image-sha256:([0-9a-fA-F]{64})\]")
IMAGE_ALIAS_PATTERN = re.compile(r"\[image-(\d+)\]")
SIMPLE_TIME_GAP_SECONDS = 3 * 60
URL_READ_BLOCKLIST_KEYWORDS = ("qq.com.cn",)
BLOCKED_URL_MESSAGE = "禁止读取的url"

_CONVERSATION_LOCKS: dict[str, Lock] = {}
_CONVERSATION_LOCKS_GUARD = Lock()


def _merge_str_dicts(
    left: dict[str, str] | None,
    right: dict[str, str] | None,
) -> dict[str, str]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


def _merge_float_dicts(
    left: dict[str, float] | None,
    right: dict[str, float] | None,
) -> dict[str, float]:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class AgentState(TypedDict, total=False):
    conversation_id: str
    run_started_at_monotonic: float
    user_message: str
    user_storage_message: str
    user_message_normalized: str
    images: list[ImageInput]
    assistant_reply_sent: bool
    working_text_base: str
    url_appendix: str
    vision_appendix: str
    route_choice: str
    route_choices: list[str]
    active_route_id: str
    memory_gate_result: str
    route_decision: RouteDecision
    execution_plan: ExecutionPlan
    url_aliases: dict[str, str]
    url_to_alias: dict[str, str]
    image_aliases: list[str]
    image_hashes: list[str]
    all_image_aliases: list[str]
    all_image_hashes: list[str]
    image_hash_to_alias: dict[str, str]
    debug: bool
    long_term_memory: str
    short_term_context: str
    working_text: str
    reply: str
    reply_lite_attempt: int
    reply_lite_check_result: str
    reply_lite_retry_feedback: str
    output_events: list[OutputEvent]
    delivered_outputs: list[OutputEvent]
    reply_completed_ms: float
    step_attachments: Annotated[dict[str, str], _merge_str_dicts]
    step_durations_ms: Annotated[dict[str, float], _merge_float_dicts]


@dataclass(slots=True)
class ReplyFirstHandle:
    conversation_id: str
    state: AgentState
    finalize_plan: ExecutionPlan


class _BackgroundAsyncRunner:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(
            target=self._run_loop, name="kisaragirin-async-runner", daemon=True
        )
        self._guard = Lock()
        self._closed = False
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro: Any) -> Any:
        with self._guard:
            if self._closed:
                raise RuntimeError("Background async runner is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def close(self) -> None:
        with self._guard:
            if self._closed:
                return
            self._closed = True

        async def _shutdown() -> None:
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._loop.shutdown_asyncgens()
            await self._loop.shutdown_default_executor()

        try:
            future = asyncio.run_coroutine_threadsafe(_shutdown(), self._loop)
            future.result(timeout=10)
        except Exception:
            pass
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
            self._loop.close()


class KisaragiAgent:
    def __init__(
        self, config: AgentConfig, tools: Sequence[BaseTool] | None = None
    ) -> None:
        self._config = config
        self._logger = logging.getLogger("kisaragirin.agent")
        self._nonebot_logger: Any | None = None
        try:
            from nonebot import logger as nonebot_logger

            self._nonebot_logger = nonebot_logger
        except Exception:
            self._nonebot_logger = None
        self._memory_store = SQLiteMemoryStore(config.memory_db_path)
        self._models = self._build_models(config)
        self._crawler_cls = None
        self._crawler_lock = Lock()
        self._async_runner = _BackgroundAsyncRunner()
        self._close_lock = Lock()
        self._closed = False

        self._tools = (
            list(tools)
            if tools is not None
            else build_default_tools(
                self._crawl_url_text,
                exa_api_key=config.exa_api_key,
                brave_search_api_key=config.brave_search_api_key,
                serpapi_api_key=config.serpapi_api_key,
            )
        )
        self._tool_map = {tool.name: tool for tool in self._tools}

    def run(self, request: ConversationRequest) -> ConversationResponse:
        conversation_lock = self._get_conversation_lock(request.conversation_id)
        with conversation_lock:
            initial_state = self._build_initial_state(request)
            started_at = time.perf_counter()
            state_after_route = self._run_route_selection(initial_state)
            result = self._run_selected_routes(state_after_route)
            delivered_output_ids = [
                output.event_id for output in (result.get("output_events") or [])
            ]
            finalize_plan = self._build_finalize_execution_plan(
                result.get("route_decision")
            )
            if finalize_plan.graph_spec.nodes:
                finalize_graph = self._build_graph_for_execution_plan(finalize_plan)
                result = finalize_graph.invoke(
                    self._state_with_delivery_results(
                        result,
                        delivered_output_ids=delivered_output_ids,
                    )
                )
            total_ms = (time.perf_counter() - started_at) * 1000
            self._log_performance_report(
                conversation_id=request.conversation_id,
                step_durations_ms=result.get("step_durations_ms"),
                reply_completed_ms=result.get("reply_completed_ms"),
                total_ms=total_ms,
            )
            outputs = list(result.get("output_events") or [])
            return ConversationResponse(
                reply=self._join_output_texts(outputs),
                outputs=outputs,
                cancelled=not outputs,
            )

    async def arun(self, request: ConversationRequest) -> ConversationResponse:
        # Keep event loop responsive in async callers by running sync graph in a worker thread.
        return await asyncio.to_thread(self.run, request)

    def _build_initial_state(self, request: ConversationRequest) -> AgentState:
        route_decision = self._resolve_route_decision(request)
        execution_plan = build_route_selection_plan(route_decision)
        return {
            "conversation_id": request.conversation_id,
            "run_started_at_monotonic": time.perf_counter(),
            "user_message": request.message,
            "user_storage_message": request.storage_message or request.message,
            "images": list(request.images),
            "assistant_reply_sent": True,
            "working_text_base": "",
            "url_appendix": "",
            "vision_appendix": "",
            "route_choice": "",
            "route_choices": [],
            "active_route_id": "",
            "memory_gate_result": "",
            "route_decision": route_decision,
            "execution_plan": execution_plan,
            "debug": request.debug,
            "output_events": [],
            "delivered_outputs": [],
            "step_attachments": {},
        }

    def _resolve_route_decision(self, request: ConversationRequest) -> RouteDecision:
        return build_default_route_decision()

    def _resolve_execution_plan(
        self,
        route_decision: RouteDecision,
        route_id: str | None = None,
        *,
        include_prelude: bool = True,
        include_route_selector: bool = True,
        include_finalize: bool = True,
    ) -> ExecutionPlan:
        return build_execution_plan(
            route_decision,
            route_id=route_id,
            include_prelude=include_prelude,
            include_route_selector=include_route_selector,
            include_finalize=include_finalize,
        )

    def _run_route_selection(self, initial_state: AgentState) -> AgentState:
        route_decision = initial_state.get("route_decision")
        if route_decision is None:
            raise RuntimeError("missing route_decision in initial state")
        route_selection_plan = build_route_selection_plan(route_decision)
        route_selection_graph = self._build_graph_for_execution_plan(
            route_selection_plan
        )
        state_after_route = route_selection_graph.invoke(initial_state)
        normalized_route_choices = list(
            normalize_route_ids(state_after_route.get("route_choices") or [])
        )
        return {
            **state_after_route,
            "route_choices": normalized_route_choices,
            "execution_plan": route_selection_plan,
        }

    def _run_selected_routes(self, state: AgentState) -> AgentState:
        route_decision = state.get("route_decision")
        if route_decision is None:
            raise RuntimeError("missing route_decision in state")

        raw_route_choices = state.get("route_choices")
        if raw_route_choices is None:
            route_choices = list(route_decision.default_route_choices)
        else:
            route_choices = list(normalize_route_ids(raw_route_choices))
        aggregated_state: AgentState = {
            **state,
            "route_choices": route_choices,
            "output_events": [],
        }
        output_events: list[OutputEvent] = []
        all_step_attachments = dict(state.get("step_attachments", {}))
        all_step_durations = dict(state.get("step_durations_ms", {}))
        max_reply_completed_ms = float(state.get("reply_completed_ms", 0.0) or 0.0)

        for route_index, route_id in enumerate(route_choices):
            execution_plan = self._resolve_execution_plan(
                route_decision,
                route_id=route_id,
                include_prelude=False,
                include_route_selector=False,
                include_finalize=False,
            )
            reply_output_key = self._reply_output_key_for_execution_plan(execution_plan)
            route_state: AgentState = {
                **state,
                "route_choice": route_id,
                "active_route_id": route_id,
                "route_choices": route_choices,
                "working_text": self._route_scoped_working_text(state, route_id),
                "execution_plan": execution_plan,
                "reply": "",
                "output_events": [],
                "delivered_outputs": [],
                "step_attachments": {},
                "step_durations_ms": {},
            }
            execution_graph = self._build_graph_for_execution_plan(execution_plan)
            route_result = execution_graph.invoke(route_state)

            prefixed_attachments = self._prefix_state_map(
                route_result.get("step_attachments"),
                prefix=route_id,
            )
            prefixed_durations = self._prefix_state_map(
                route_result.get("step_durations_ms"),
                prefix=route_id,
            )
            all_step_attachments.update(prefixed_attachments)
            for key, value in prefixed_durations.items():
                try:
                    all_step_durations[key] = float(value)
                except Exception:
                    continue

            reply_text = ""
            if reply_output_key:
                reply_text = str(route_result.get(reply_output_key, "") or "").strip()
            if reply_text and reply_text != "bot选择沉默":
                output_event = OutputEvent(
                    event_id=f"{route_id}:{route_index}",
                    event_type="reply",
                    route_id=route_id,
                    content=reply_text,
                    order=route_index,
                    dedupe_key=f"{route_id}:{route_index}",
                )
                output_events.append(output_event)

            reply_completed_ms = route_result.get("reply_completed_ms")
            if isinstance(reply_completed_ms, (int, float)):
                max_reply_completed_ms = max(
                    max_reply_completed_ms,
                    float(reply_completed_ms),
                )

        aggregated_state["step_attachments"] = all_step_attachments
        aggregated_state["step_durations_ms"] = all_step_durations
        aggregated_state["reply_completed_ms"] = max_reply_completed_ms
        aggregated_state["output_events"] = output_events
        aggregated_state["reply"] = self._join_output_texts(output_events)
        return aggregated_state

    def _route_scoped_working_text(self, state: AgentState, route_id: str) -> str:
        route_decision = state.get("route_decision")
        if route_decision is None:
            raise RuntimeError("missing route_decision in state")
        route_instruction = (
            route_decision.route_processing_instructions.get(route_id, "").strip()
            or "(empty)"
        )
        base_working_text = str(state.get("working_text", ""))
        return (
            f"{base_working_text}\n\n"
            "[ACTIVE-ROUTE]\n"
            f"route_id: {route_id}\n"
            "[ROUTE-INSTRUCTION]\n"
            f"{route_instruction}"
        )

    @staticmethod
    def _prefix_state_map(
        source: dict[str, Any] | None,
        *,
        prefix: str,
    ) -> dict[str, Any]:
        if not isinstance(source, dict):
            return {}
        return {f"{prefix}.{key}": value for key, value in source.items()}

    @staticmethod
    def _join_output_texts(outputs: Sequence[OutputEvent]) -> str:
        texts = [output.content.strip() for output in outputs if output.content.strip()]
        return "\n\n".join(texts)

    def _state_with_delivery_results(
        self,
        state: AgentState,
        *,
        delivered_output_ids: Sequence[str],
    ) -> AgentState:
        delivered_id_set = {str(item) for item in delivered_output_ids}
        delivered_outputs = [
            output
            for output in (state.get("output_events") or [])
            if output.event_id in delivered_id_set
        ]
        return {
            **state,
            "assistant_reply_sent": bool(delivered_outputs),
            "delivered_outputs": delivered_outputs,
            "reply": self._join_output_texts(delivered_outputs),
        }

    def _execution_steps(
        self, execution_plan: ExecutionPlan
    ) -> list[tuple[str, str, Any]]:
        return [
            (step.step_name, step.node_name, step.handler)
            for step in resolve_all_steps(
                execution_plan,
                self._step_implementations(),
            )
        ]

    def _reply_output_key_for_execution_plan(
        self,
        execution_plan: ExecutionPlan,
    ) -> str | None:
        reply_output_keys = [
            step.output_key
            for step in resolve_all_steps(
                execution_plan,
                self._step_implementations(),
            )
            if step.output_key
        ]
        if not reply_output_keys:
            return None
        return str(reply_output_keys[-1])

    def _step_implementations(self) -> StepImplementationRegistry:
        return {
            "prepare": {
                "default": lambda state: run_prepare(self, state),
            },
            "url": {
                "default": lambda state: run_urls(self, state),
            },
            "vision": {
                "default": lambda state: run_vision(self, state),
            },
            "enrich_merge": {
                "default": lambda state: run_enrich_merge(self, state),
            },
            "route": {
                "default": lambda state: run_route(self, state),
            },
            "tools": {
                "default": lambda state: run_tools(self, state),
            },
            "reply": {
                "default": lambda state: run_reply(self, state),
                "lite": lambda state: run_reply_lite(self, state),
            },
            "reply_lite_check": {
                "default": lambda state: run_reply_lite_check(self, state),
            },
            "memory_gate": {
                "default": lambda state: run_memory_gate(self, state),
            },
            "memory": {
                "default": lambda state: run_memory(self, state),
            },
        }

    def _build_graph_for_execution_plan(self, execution_plan: ExecutionPlan):
        return build_graph_for_execution_plan(
            state_type=AgentState,
            execution_plan=execution_plan,
            implementations=self._step_implementations(),
            wrap_step=self._with_step_timing,
        )

    async def arun_reply_first(
        self, request: ConversationRequest
    ) -> tuple[ConversationResponse, ReplyFirstHandle]:
        return await asyncio.to_thread(self._run_reply_first, request)

    def _run_reply_first(
        self,
        request: ConversationRequest,
    ) -> tuple[ConversationResponse, ReplyFirstHandle]:
        conversation_lock = self._get_conversation_lock(request.conversation_id)
        with conversation_lock:
            state = self._build_initial_state(request)
            state = self._run_route_selection(state)
            state = self._run_selected_routes(state)
            output_events = list(state.get("output_events") or [])
            return ConversationResponse(
                reply=self._join_output_texts(output_events),
                outputs=output_events,
                cancelled=not output_events,
            ), ReplyFirstHandle(
                conversation_id=request.conversation_id,
                state=state,
                finalize_plan=self._build_finalize_execution_plan(
                    state.get("route_decision")
                ),
            )

    async def afinalize_reply_first(
        self,
        handle: ReplyFirstHandle,
        *,
        delivered_output_ids: Sequence[str],
    ) -> None:
        await asyncio.to_thread(
            self._finalize_reply_first,
            handle,
            delivered_output_ids,
        )

    def _finalize_reply_first(
        self,
        handle: ReplyFirstHandle,
        delivered_output_ids: Sequence[str],
    ) -> None:
        conversation_lock = self._get_conversation_lock(handle.conversation_id)
        with conversation_lock:
            state = self._state_with_delivery_results(
                handle.state,
                delivered_output_ids=delivered_output_ids,
            )
            if handle.finalize_plan.graph_spec.nodes:
                finalize_graph = self._build_graph_for_execution_plan(
                    handle.finalize_plan
                )
                state = finalize_graph.invoke(state)
            run_started_at = state.get("run_started_at_monotonic")
            total_ms = 0.0
            if isinstance(run_started_at, (int, float)):
                total_ms = (time.perf_counter() - float(run_started_at)) * 1000
            self._log_performance_report(
                conversation_id=handle.conversation_id,
                step_durations_ms=state.get("step_durations_ms"),
                reply_completed_ms=state.get("reply_completed_ms"),
                total_ms=total_ms,
            )

    @staticmethod
    def _build_finalize_execution_plan(
        route_decision: RouteDecision | None,
    ) -> ExecutionPlan:
        if route_decision is None:
            raise RuntimeError("missing route_decision for finalize plan")
        return ExecutionPlan(
            route_id="finalize",
            shared_prelude_graph=EMPTY_GRAPH,
            route_selector_graph=EMPTY_GRAPH,
            route_graph=EMPTY_GRAPH,
            shared_finalize_graph=route_decision.shared_finalize_graph,
            graph_spec=route_decision.shared_finalize_graph,
            phase_variants={},
        )

    def clear_conversation(self, conversation_id: str) -> None:
        conversation_lock = self._get_conversation_lock(conversation_id)
        with conversation_lock:
            self._memory_store.clear_conversation(conversation_id)

    def clear_short_term_memory(self, conversation_id: str) -> None:
        conversation_lock = self._get_conversation_lock(conversation_id)
        with conversation_lock:
            self._memory_store.clear_short_term(conversation_id)

    def clear_long_term_memory(self, conversation_id: str) -> None:
        conversation_lock = self._get_conversation_lock(conversation_id)
        with conversation_lock:
            self._memory_store.clear_long_term(conversation_id)

    def set_self_name(self, self_name: str) -> None:
        self._config.self_name = str(self_name or "").strip() or "assistant"

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
        self._async_runner.close()
        self._memory_store.close()

    def __enter__(self) -> "KisaragiAgent":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def _build_models(self, config: AgentConfig) -> dict[str, BaseChatModel]:
        models: dict[str, BaseChatModel] = {}
        for model_id, model_cfg in config.models.items():
            provider = model_cfg.provider.strip().lower() or "openai"
            chat_cls = self._resolve_chat_model_class(provider)
            init_kwargs = self._build_model_init_kwargs(model_cfg)
            models[model_id] = chat_cls(**init_kwargs)

        required_ids = {
            config.step_models.summarize,
            config.step_models.vision,
            config.step_models.tool,
            config.step_models.reply,
            config.step_models.memory,
        }
        missing = [mid for mid in required_ids if mid not in models]
        if missing:
            raise ValueError(f"Missing model config ids: {missing}")
        return models

    def _with_step_timing(
        self,
        metric_name: str,
        step_fn: Any,
    ):
        def _wrapped(state: AgentState) -> dict[str, Any]:
            started_at = time.perf_counter()
            result = step_fn(state)
            elapsed_ms = (time.perf_counter() - started_at) * 1000

            merged_step_durations: dict[str, float] = {}
            for source in (
                state.get("step_durations_ms"),
                result.get("step_durations_ms"),
            ):
                if isinstance(source, dict):
                    for key, value in source.items():
                        try:
                            merged_step_durations[str(key)] = float(value)
                        except Exception:
                            continue
            merged_step_durations[metric_name] = elapsed_ms

            merged_result = dict(result)
            merged_result["step_durations_ms"] = merged_step_durations
            if (
                metric_name.startswith("reply")
                and "reply_completed_ms" not in merged_result
            ):
                run_started_at = state.get("run_started_at_monotonic")
                if isinstance(run_started_at, (int, float)):
                    merged_result["reply_completed_ms"] = (
                        time.perf_counter() - float(run_started_at)
                    ) * 1000
            return merged_result

        return _wrapped

    def _summarize_url(self, alias: str, page_text: str) -> str:
        model = self._model(self._config.step_models.summarize)
        content_for_summary = page_text[: self._config.max_crawl_chars]
        summary_msg = model.invoke(
            [
                SystemMessage(content=self._system_prompt("summarize")),
                HumanMessage(
                    content=URL_SUMMARY_PROMPT_TEMPLATE.format(
                        alias=alias,
                        content=content_for_summary,
                    )
                ),
            ]
        )
        summary = self._message_to_text(summary_msg.content)
        if len(summary) > self._config.max_summary_chars:
            summary = summary[: self._config.max_summary_chars] + "\n...<truncated>"
        return summary

    def _get_or_create_url_summary(
        self,
        *,
        alias: str,
        url: str,
        summary_by_url: dict[str, str],
    ) -> tuple[str, str, int]:
        if self._is_url_blocked(url):
            summary_by_url[url] = BLOCKED_URL_MESSAGE
            return BLOCKED_URL_MESSAGE, "blocked", 0

        existing = summary_by_url.get(url)
        if existing is not None:
            return existing, "hit", 0

        cached = self._memory_store.get_url_summary(url)
        if cached is not None:
            summary_by_url[url] = cached
            return cached, "hit", 0

        page_text = self._crawl_url_text(
            url=url, max_chars=self._config.max_crawl_chars
        )
        summary = self._summarize_url(alias=alias, page_text=page_text)
        self._memory_store.set_url_summary(url, summary)
        summary_by_url[url] = summary
        return summary, "miss", len(page_text)

    def _get_or_create_image_description(
        self,
        *,
        image: ImageInput,
        image_hash: str,
        description_by_hash: dict[str, str],
    ) -> str:
        if image_hash:
            existing = description_by_hash.get(image_hash)
            if existing is not None:
                return existing

            cached = self._memory_store.get_image_description(image_hash)
            if cached is not None:
                description_by_hash[image_hash] = cached
                return cached

        description = self._describe_image(image)
        if image_hash:
            self._memory_store.set_image_description(image_hash, description)
            description_by_hash[image_hash] = description
        return description

    def _describe_image(self, image: ImageInput) -> str:
        model = self._model(self._config.step_models.vision)
        content: list[str | dict[str, object]] = []
        animation_frames = list(image.animation_frames or [])
        if animation_frames:
            content.append(
                {
                    "type": "text",
                    "text": ANIMATED_VISION_DESCRIPTION_PROMPT.format(
                        frame_count=len(animation_frames)
                    ),
                }
            )
            for frame in animation_frames:
                try:
                    frame_url = frame.to_model_url()
                except Exception as exc:
                    return f"Animated image frame payload error: {exc}"
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": frame_url},
                    }
                )
        else:
            try:
                image_url = image.to_model_url()
            except Exception as exc:
                return f"Image payload error: {exc}"
            content.extend(
                [
                    {
                        "type": "text",
                        "text": VISION_DESCRIPTION_PROMPT,
                    },
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]
            )

        msg = model.invoke(
            [
                SystemMessage(content=self._system_prompt("vision")),
                HumanMessage(content=content),
            ]
        )
        return self._message_to_text(msg.content)

    def _invoke_tool(self, tool_name: str, tool_args: dict[str, Any] | Any) -> str:
        tool = self._tool_map.get(tool_name)
        if not tool:
            return f"Tool '{tool_name}' is not available."
        try:
            result = tool.invoke(tool_args)
            if isinstance(result, str):
                return result
            return json.dumps(result, ensure_ascii=False)
        except Exception as exc:
            return f"Tool '{tool_name}' execution error: {exc}"

    def _crawl_url_text(self, url: str, max_chars: int) -> str:
        if self._is_url_blocked(url):
            return BLOCKED_URL_MESSAGE

        crawler_cls = self._get_crawler_cls()

        async def _crawl() -> str:
            crawler_config = self._config.crawler
            browser_kwargs: dict[str, Any] = {
                "headless": crawler_config.headless,
                "verbose": crawler_config.verbose,
            }
            user_data_dir = str(crawler_config.user_data_dir or "").strip()
            if user_data_dir:
                browser_kwargs["user_data_dir"] = str(Path(user_data_dir).expanduser())

            async with crawler_cls(config=BrowserConfig(**browser_kwargs)) as crawler:
                result = await crawler.arun(url=url)
                text = self._extract_crawl_text(result)
                if len(text) > max_chars:
                    return text[:max_chars] + "\n...<truncated>"
                return text

        try:
            return self._async_runner.run(_crawl())
        except Exception as exc:
            return f"crawl failed for {url}: {exc}"

    def _get_crawler_cls(self):
        if self._crawler_cls is not None:
            return self._crawler_cls
        with self._crawler_lock:
            if self._crawler_cls is not None:
                return self._crawler_cls
            if "CRAWL4_AI_BASE_DIRECTORY" not in os.environ:
                base_dir = Path(self._config.memory_db_path).resolve().parent
                os.environ["CRAWL4_AI_BASE_DIRECTORY"] = str(base_dir)
            from crawl4ai import AsyncWebCrawler

            self._crawler_cls = AsyncWebCrawler
            return self._crawler_cls

    def _log_step_debug(self, state: AgentState, step: str, content: str) -> None:
        if not bool(state.get("debug")):
            return
        conversation_id = str(state.get("conversation_id", "?"))
        self._log_info(
            "[DEBUG][%s][conversation=%s]\n%s", step, conversation_id, content
        )

    def _log_model_messages(
        self,
        state: AgentState,
        step: str,
        messages: Sequence[BaseMessage],
    ) -> None:
        if not bool(state.get("debug")):
            return
        conversation_id = str(state.get("conversation_id", "?"))
        rendered = self._render_debug_messages(messages)
        self._log_info(
            "[DEBUG][%s.llm_input][conversation=%s]\n%s",
            step,
            conversation_id,
            rendered,
        )

    def _render_debug_messages(self, messages: Sequence[BaseMessage]) -> str:
        blocks: list[str] = []
        for index, message in enumerate(messages, start=1):
            blocks.append(self._render_debug_message(index, message))
        return "\n\n".join(blocks)

    def _render_debug_message(self, index: int, message: BaseMessage) -> str:
        role = str(getattr(message, "type", message.__class__.__name__)).upper()
        content = self._render_debug_content(getattr(message, "content", ""))
        _ = index
        blocks = [f"[{role}]", content]
        if isinstance(message, AIMessage) and message.tool_calls:
            blocks.append("[TOOL-CALLS]")
            blocks.append(json.dumps(message.tool_calls, ensure_ascii=False, indent=2))
        if isinstance(message, ToolMessage):
            blocks.append("[TOOL-NAME]")
            blocks.append(str(getattr(message, "name", "") or "(unknown)"))
            blocks.append("[TOOL-CALL-ID]")
            blocks.append(str(getattr(message, "tool_call_id", "") or "(unknown)"))
        return "\n".join(blocks)

    def _render_debug_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        try:
            return json.dumps(content, ensure_ascii=False, indent=2)
        except TypeError:
            return self._message_to_text(content)

    def _log_performance_report(
        self,
        *,
        conversation_id: str,
        step_durations_ms: dict[str, float] | None,
        reply_completed_ms: float | None,
        total_ms: float,
    ) -> None:
        return

    def _log_info(self, fmt: str, *args: Any) -> None:
        message = fmt % args if args else fmt
        if self._nonebot_logger is not None:
            self._nonebot_logger.info(message)
            return
        self._logger.info(message)

    @staticmethod
    def _get_conversation_lock(conversation_id: str) -> Lock:
        with _CONVERSATION_LOCKS_GUARD:
            lock = _CONVERSATION_LOCKS.get(conversation_id)
            if lock is None:
                lock = Lock()
                _CONVERSATION_LOCKS[conversation_id] = lock
            return lock

    @staticmethod
    def _extract_crawl_text(result: Any) -> str:
        markdown_v2 = getattr(result, "markdown_v2", None)
        candidates = [
            getattr(result, "markdown", None),
            getattr(markdown_v2, "raw_markdown", None),
            getattr(result, "cleaned_html", None),
            getattr(result, "html", None),
            getattr(result, "text", None),
        ]
        for item in candidates:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return "(no extractable text from crawl result)"

    @staticmethod
    def _replace_urls_with_aliases(text: str) -> tuple[str, dict[str, str]]:
        alias_to_url: dict[str, str] = {}
        url_to_alias: dict[str, str] = {}
        parts: list[str] = []
        last_end = 0
        next_index = 1

        for match in URL_PATTERN.finditer(text):
            raw = match.group(1)
            start, end = match.span(1)
            cleaned = KisaragiAgent._normalize_url_from_match(raw)
            if not cleaned:
                continue
            alias = url_to_alias.get(cleaned)
            if alias is None:
                alias = KisaragiAgent._format_url_alias(next_index, cleaned)
                next_index += 1
                url_to_alias[cleaned] = alias
                alias_to_url[alias] = cleaned
            suffix = raw[len(cleaned) :]
            parts.append(text[last_end:start])
            parts.append(alias + suffix)
            last_end = end

        if not parts:
            return text, alias_to_url
        parts.append(text[last_end:])
        return "".join(parts), alias_to_url

    @staticmethod
    def _format_url_alias(index: int, url: str) -> str:
        preview = url[:40].replace("]", "%5D")
        if len(url) > 40:
            preview += "...(cut off)"
        return f"[url-{index}|{preview}]"

    @staticmethod
    def _normalize_url_from_match(raw: str) -> str:
        cleaned = raw.rstrip(".,;:!?)]}>'\"")
        if not cleaned:
            return ""
        normalized = cleaned if "://" in cleaned else f"http://{cleaned}"
        parsed = urlsplit(normalized)
        if parsed.scheme.lower() not in {"http", "https", "ftp"}:
            return ""
        if not parsed.netloc:
            return ""
        return cleaned

    @staticmethod
    def _is_url_blocked(url: str) -> bool:
        normalized = str(url).strip().lower()
        if not normalized:
            return False
        return any(keyword in normalized for keyword in URL_READ_BLOCKLIST_KEYWORDS)

    def _model(self, model_id: str) -> BaseChatModel:
        return self._models[model_id]

    def _resolve_chat_model_class(self, provider: str):
        if provider == "openai":
            try:
                module = import_module("langchain_openai")
                return getattr(module, "ChatOpenAI")
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Provider 'openai' requires package 'langchain-openai'."
                ) from exc
        if provider == "siliconflow":
            try:
                module = import_module("langchain_siliconflow")
                return getattr(module, "ChatSiliconFlow")
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "Provider 'siliconflow' requires package 'langchain-siliconflow'."
                ) from exc
        raise ValueError(
            f"Unsupported provider '{provider}'. Supported providers: openai, siliconflow"
        )

    @staticmethod
    def _build_model_init_kwargs(model_cfg: Any) -> dict[str, Any]:
        raw_kwargs: dict[str, Any] = {
            "base_url": model_cfg.base_url,
            "api_key": model_cfg.api_key,
            "model": model_cfg.model,
            "temperature": model_cfg.temperature,
            "timeout": model_cfg.timeout,
            "extra_body": model_cfg.extra_body,
            "model_kwargs": dict(model_cfg.model_kwargs),
        }
        merged = {k: v for k, v in raw_kwargs.items() if v is not None}
        merged.update(dict(model_cfg.client_kwargs))
        return merged

    def _system_prompt(self, step: str) -> str:
        sections: list[str] = []
        instruction = STEP_SYSTEM_INSTRUCTIONS.get(step, "")
        if instruction:
            sections.append(
                f"---\nThis is a scenario task evaluation project used to test the capabilities of LLM. After reading the task file, complete the corresponding tasks according to the instructions within, and output the final results.\n---TASK FILE CONTENT---\n\n[INSTRUCTION:{step}]\n{instruction}"
            )
        if step not in {"summarize", "vision"} and self._config.prompts.persona.strip():
            sections.append(
                "---Additional Task Requirements---\n[OUTPUT_STYLE.PERSONA]\n"
                + self._config.prompts.persona.strip()
            )
        if sections:
            return ("\n\n".join(sections)) + "\n---\n"
        return f"You are in step '{step}'.\n---\n"

    @staticmethod
    def _compute_image_sha256(image: ImageInput) -> str:
        raw = KisaragiAgent._decode_image_bytes(image)
        if raw is None:
            return ""
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _decode_image_bytes(image: ImageInput) -> bytes | None:
        if image.base64_data:
            try:
                return base64.b64decode(image.base64_data, validate=False)
            except Exception:
                return None
        if image.url and image.url.startswith("data:") and "," in image.url:
            header, data = image.url.split(",", 1)
            if ";base64" in header:
                try:
                    return base64.b64decode(data, validate=False)
                except Exception:
                    return None
            return unquote_to_bytes(data)
        return None

    @staticmethod
    def _format_short_term_context(
        messages: list[ShortTermMessage],
        *,
        message_format: str = "yaml",
        self_name: str = "assistant",
        short_term_image_refs: dict[float, dict[int, str]] | None = None,
        short_term_hash_to_alias: dict[str, str] | None = None,
        short_term_url_to_alias: dict[str, str] | None = None,
    ) -> str:
        if not messages:
            return "(empty)"
        if message_format == "simple":
            blocks: list[str] = []
            merged_messages: list[dict[str, object]] = []

            def _flush_merged_messages() -> None:
                if not merged_messages:
                    return
                blocks.append(KisaragiAgent._render_simple_payload(list(merged_messages)))
                merged_messages.clear()

            for item in messages:
                content = KisaragiAgent._replace_legacy_image_hash_aliases(
                    item.content,
                    hash_to_alias=short_term_hash_to_alias,
                )
                refs_by_index = (
                    short_term_image_refs.get(item.created_at, {})
                    if item.role == "user" and short_term_image_refs
                    else {}
                )
                if refs_by_index and short_term_hash_to_alias:
                    content = KisaragiAgent._replace_image_aliases_with_short_aliases(
                        content,
                        refs_by_index=refs_by_index,
                        hash_to_alias=short_term_hash_to_alias,
                    )
                if short_term_url_to_alias:
                    content = KisaragiAgent._replace_urls_with_known_aliases(
                        content,
                        url_to_alias=short_term_url_to_alias,
                    )
                payload = KisaragiAgent._coerce_stored_message_payload(
                    role=item.role,
                    content=content,
                    created_at=item.created_at,
                    self_name=self_name,
                )
                payload_messages = (
                    KisaragiAgent._payload_message_list(payload)
                    if payload is not None
                    else []
                )
                if payload_messages:
                    merged_messages.extend(payload_messages)
                    continue
                _flush_merged_messages()
                if content.strip():
                    blocks.append(content)
            _flush_merged_messages()
            if not blocks:
                return "(empty)"
            return "\n\n".join(block for block in blocks if block.strip())
        blocks: list[str] = []
        for item in messages:
            content = KisaragiAgent._replace_legacy_image_hash_aliases(
                item.content,
                hash_to_alias=short_term_hash_to_alias,
            )
            refs_by_index = (
                short_term_image_refs.get(item.created_at, {})
                if item.role == "user" and short_term_image_refs
                else {}
            )
            if refs_by_index and short_term_hash_to_alias:
                content = KisaragiAgent._replace_image_aliases_with_short_aliases(
                    content,
                    refs_by_index=refs_by_index,
                    hash_to_alias=short_term_hash_to_alias,
                )
            if short_term_url_to_alias:
                content = KisaragiAgent._replace_urls_with_known_aliases(
                    content,
                    url_to_alias=short_term_url_to_alias,
                )
            formatted_content = KisaragiAgent._format_stored_short_term_message(
                role=item.role,
                content=content,
                created_at=item.created_at,
                message_format=message_format,
                self_name=self_name,
            )
            blocks.append(formatted_content)
        return "\n\n".join(block for block in blocks if block.strip())

    @staticmethod
    def _stored_payload_messages(content: str) -> list[dict[str, object]]:
        payload = KisaragiAgent._try_parse_stored_message_payload(content)
        if payload is None:
            return []
        return KisaragiAgent._payload_message_list(payload)

    @staticmethod
    def _payload_message_list(payload: dict[str, object]) -> list[dict[str, object]]:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return []
        return [cast(dict[str, object], item) for item in messages if isinstance(item, dict)]

    @staticmethod
    def _format_stored_short_term_message(
        *,
        role: str,
        content: str,
        created_at: float,
        message_format: str,
        self_name: str,
    ) -> str:
        if role == "user":
            if message_format == "simple":
                return KisaragiAgent._render_stored_message_content(
                    content=content,
                    created_at=created_at,
                    role=role,
                    self_name=self_name,
                )
            return content
        if role == "assistant":
            return KisaragiAgent._render_stored_message_content(
                content=content,
                created_at=created_at,
                role=role,
                message_format=message_format,
                self_name=self_name,
            )
        return content

    @staticmethod
    def _render_stored_message_content(
        *,
        content: str,
        created_at: float,
        role: str,
        message_format: str = "simple",
        self_name: str = "assistant",
    ) -> str:
        payload = KisaragiAgent._coerce_stored_message_payload(
            role=role,
            content=content,
            created_at=created_at,
            self_name=self_name,
        )
        if payload is None:
            return content
        if message_format == "simple":
            normalized_messages = KisaragiAgent._payload_message_list(payload)
            if not normalized_messages:
                return content
            return KisaragiAgent._render_simple_payload(normalized_messages)
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()

    @staticmethod
    def _coerce_stored_message_payload(
        *,
        role: str,
        content: str,
        created_at: float,
        self_name: str,
    ) -> dict[str, object] | None:
        payload = KisaragiAgent._try_parse_stored_message_payload(content)
        if payload is not None:
            if role == "assistant":
                return KisaragiAgent._mark_payload_as_self_message(
                    payload,
                    self_name=self_name,
                )
            return payload
        if role != "assistant":
            return None
        normalized_content = str(content or "").strip()
        if not normalized_content:
            return None
        return KisaragiAgent._build_assistant_storage_payload(
            normalized_content,
            self_name=self_name,
            created_at=created_at,
        )

    @staticmethod
    def _build_assistant_storage_message(
        content: str,
        *,
        self_name: str,
        created_at: float | None = None,
    ) -> str:
        payload = KisaragiAgent._build_assistant_storage_payload(
            content,
            self_name=self_name,
            created_at=created_at,
        )
        return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).strip()

    @staticmethod
    def _build_assistant_storage_payload(
        content: str,
        *,
        self_name: str,
        created_at: float | None = None,
    ) -> dict[str, object]:
        normalized_content = str(content or "").strip()
        normalized_name = str(self_name or "").strip() or "assistant"
        sent_at = float(created_at) if created_at is not None else time.time()
        message: dict[str, object] = {
            "message_id": f"assistant-{int(sent_at * 1000)}",
            "sent_at_local": datetime.fromtimestamp(sent_at).astimezone().isoformat(),
            "role": "assistant",
            "sender": {
                "id": "assistant",
                "name": normalized_name,
                "is_me": True,
                "role": "assistant",
            },
            "mentioned_bot": False,
            "segments": [
                {
                    "type": "text",
                    "text": normalized_content,
                }
            ],
        }
        if normalized_content:
            message["merged_text"] = normalized_content
        return {
            "schema_version": 1,
            "source": "kisaragirin",
            "messages": [message],
        }

    @staticmethod
    def _mark_payload_as_self_message(
        payload: dict[str, object],
        *,
        self_name: str,
    ) -> dict[str, object]:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return payload
        normalized_name = str(self_name or "").strip() or "assistant"
        normalized_messages: list[dict[str, object]] = []
        for raw_message in messages:
            if not isinstance(raw_message, dict):
                continue
            message = dict(cast(dict[str, object], raw_message))
            sender_raw = message.get("sender")
            sender = dict(cast(dict[str, object], sender_raw)) if isinstance(sender_raw, dict) else {}
            sender["id"] = str(sender.get("id", "") or "assistant")
            sender["name"] = str(sender.get("name", "") or normalized_name)
            sender["is_me"] = True
            sender["role"] = "assistant"
            message["sender"] = sender
            message["role"] = "assistant"
            normalized_messages.append(message)
        normalized_payload = dict(payload)
        normalized_payload["messages"] = normalized_messages
        return normalized_payload

    @staticmethod
    def _try_parse_stored_message_payload(content: str) -> dict[str, object] | None:
        text = str(content or "").strip()
        if not text:
            return None
        try:
            loaded = yaml.safe_load(text)
        except yaml.YAMLError:
            return None
        if not isinstance(loaded, dict):
            return None
        messages = loaded.get("messages")
        if not isinstance(messages, list):
            return None
        return cast(dict[str, object], loaded)

    @staticmethod
    def _render_simple_payload(messages: list[dict[str, object]]) -> str:
        blocks: list[str] = []
        block_started_at: datetime | None = None
        for message in messages:
            timestamp = KisaragiAgent._parse_sent_at_local(message)
            if timestamp is not None and (
                block_started_at is None
                or (timestamp - block_started_at).total_seconds() > SIMPLE_TIME_GAP_SECONDS
            ):
                blocks.append(timestamp.strftime("%Y-%m-%d %H:%M"))
                block_started_at = timestamp
            blocks.append(KisaragiAgent._render_simple_message(message))
        if not blocks:
            return "---\n---"
        return "---\n" + "\n---\n".join(blocks) + "\n---"

    @staticmethod
    def _parse_sent_at_local(message: dict[str, object]) -> datetime | None:
        raw = str(message.get("sent_at_local", "") or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    @staticmethod
    def _render_simple_message(message: dict[str, object]) -> str:
        sender_name = KisaragiAgent._message_sender_name(message)
        content, reference_lines = KisaragiAgent._render_simple_message_content(
            message,
            reply_depth=1,
        )
        prefix = "(有人@我)" if bool(message.get("mentioned_bot")) else ""
        header = f"{prefix}[{sender_name}]:"
        if content:
            header = f"{header} {content}"
        if not reference_lines:
            return header
        lines = [header]
        lines.extend(f"  {line}" for line in reference_lines)
        return "\n".join(lines)

    @staticmethod
    def _render_simple_message_content(
        message: dict[str, object],
        *,
        reply_depth: int,
    ) -> tuple[str, list[str]]:
        inline_parts: list[str] = []
        reference_lines: list[str] = []
        segments = message.get("segments")
        if not isinstance(segments, list):
            segments = []
        for raw_segment in segments:
            if not isinstance(raw_segment, dict):
                continue
            segment = cast(dict[str, object], raw_segment)
            segment_type = str(segment.get("type", "") or "").strip()
            if segment_type == "text":
                KisaragiAgent._append_inline_part(
                    inline_parts,
                    str(segment.get("text", "") or ""),
                )
                continue
            if segment_type == "at":
                KisaragiAgent._append_inline_part(
                    inline_parts,
                    str(segment.get("text", "") or ""),
                )
                continue
            if segment_type == "image":
                KisaragiAgent._append_inline_part(
                    inline_parts,
                    str(segment.get("image", "") or ""),
                )
                continue
            if segment_type == "reply":
                if reply_depth <= 0:
                    reply_id = str(segment.get("reply_to_message_id", "") or "").strip()
                    KisaragiAgent._append_inline_part(
                        inline_parts,
                        f"[reply:{reply_id or 'unknown'}]",
                    )
                    continue
                reference_lines.append(
                    KisaragiAgent._render_simple_reference_line(
                        segment,
                        reply_depth=reply_depth,
                    )
                )
                continue
            if segment_type == "forward":
                forward_lines = KisaragiAgent._render_simple_forward_lines(
                    segment,
                    reply_depth=reply_depth,
                )
                if forward_lines:
                    reference_lines.extend(forward_lines)
                    continue
                forward_id = str(segment.get("forward_id", "") or "").strip()
                KisaragiAgent._append_inline_part(
                    inline_parts,
                    f"[forward:{forward_id or 'unknown'}]",
                )
                continue

            placeholder = KisaragiAgent._render_simple_inline_segment(segment)
            if not placeholder:
                continue
            KisaragiAgent._append_inline_part(
                inline_parts,
                placeholder,
            )

        inline_text = "".join(inline_parts).strip()
        if not inline_text:
            inline_text = str(message.get("merged_text", "") or "").strip()
        return inline_text, reference_lines

    @staticmethod
    def _render_simple_reference_line(
        reply_segment: dict[str, object],
        *,
        reply_depth: int,
    ) -> str:
        nested = reply_segment.get("reply_to_message")
        reply_id = str(reply_segment.get("reply_to_message_id", "") or "").strip()
        if not isinstance(nested, dict):
            return f"[ref {reply_id or 'unknown'}]：(unavailable)"
        nested_message = cast(dict[str, object], nested)
        sender_name = KisaragiAgent._message_sender_name(nested_message)
        content, _ = KisaragiAgent._render_simple_message_content(
            nested_message,
            reply_depth=reply_depth - 1,
        )
        if not content:
            content = "(empty)"
        return f"[ref {sender_name}]：{content}"

    @staticmethod
    def _render_simple_forward_lines(
        forward_segment: dict[str, object],
        *,
        reply_depth: int,
    ) -> list[str]:
        raw_messages = forward_segment.get("forward_messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            return []

        lines: list[str] = []
        for raw_message in raw_messages:
            if not isinstance(raw_message, dict):
                continue
            message = cast(dict[str, object], raw_message)
            sender_name = KisaragiAgent._message_sender_name(message)
            content, _ = KisaragiAgent._render_simple_message_content(
                message,
                reply_depth=reply_depth - 1,
            )
            if not content:
                content = "(empty)"
            lines.append(f"[forward {sender_name}]：{content}")
        return lines

    @staticmethod
    def _message_sender_name(message: dict[str, object]) -> str:
        sender = message.get("sender")
        if not isinstance(sender, dict):
            return "unknown"
        sender_data = cast(dict[str, object], sender)
        name = str(sender_data.get("name", "") or "").strip()
        if bool(sender_data.get("is_me")) and name:
            return f"{name}(me)"
        if name:
            return name
        sender_id = str(sender_data.get("id", "") or "").strip()
        return sender_id or "unknown"

    @staticmethod
    def _append_inline_part(parts: list[str], value: str) -> None:
        text = str(value or "")
        if not text:
            return
        if not parts:
            parts.append(text)
            return
        previous = parts[-1]
        if previous.endswith((" ", "\n")) or text.startswith(
            (" ", "\n", "，", "。", "！", "？", "、", ",", ".", "!", "?")
        ):
            parts.append(text)
            return
        parts.append(f" {text}")

    @staticmethod
    def _render_simple_inline_segment(segment: dict[str, object]) -> str:
        segment_type = str(segment.get("type", "") or "").strip()
        raw = segment.get("data")
        raw_data = cast(dict[str, object], raw) if isinstance(raw, dict) else {}

        if segment_type == "face":
            name = str(segment.get("name", "") or "").strip()
            if not name:
                name = str(raw_data.get("id", "") or "").strip() or "unknown"
            return f"[face: {name}]"

        if segment_type == "record":
            return "[record: 语音]"

        if segment_type in {"video", "file"}:
            name = KisaragiAgent._segment_file_name(raw_data) or "unknown"
            return f"[{segment_type}: {name}]"

        if segment_type == "json":
            return f"[json: {KisaragiAgent._json_segment_text(raw_data)}]"

        if segment_type == "poke":
            detail = KisaragiAgent._joined_segment_detail(raw_data, keys=("type", "id")) or "unknown"
            return f"[poke: {detail}]"

        if segment_type in {"dice", "rps"}:
            result = str(raw_data.get("result", "") or "").strip() or "unknown"
            return f"[{segment_type}: {result}]"

        return ""

    @staticmethod
    def _segment_file_name(raw_data: dict[str, object]) -> str:
        for key in ("name", "file", "path", "file_id"):
            value = str(raw_data.get(key, "") or "").strip()
            if value:
                return value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return ""

    @staticmethod
    def _json_segment_text(raw_data: dict[str, object]) -> str:
        value = raw_data.get("data", "")
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        except TypeError:
            return str(value or "")

    @staticmethod
    def _joined_segment_detail(
        raw_data: dict[str, object],
        *,
        keys: tuple[str, ...],
    ) -> str:
        parts = [str(raw_data.get(key, "") or "").strip() for key in keys]
        normalized = [part for part in parts if part]
        return "/".join(normalized)

    @staticmethod
    def _replace_urls_with_known_aliases(
        text: str, *, url_to_alias: dict[str, str]
    ) -> str:
        if not text:
            return text
        if not url_to_alias:
            return text

        parts: list[str] = []
        last_end = 0
        for match in URL_PATTERN.finditer(text):
            raw = match.group(1)
            start, end = match.span(1)
            cleaned = KisaragiAgent._normalize_url_from_match(raw)
            if not cleaned:
                continue
            alias = url_to_alias.get(cleaned)
            if alias is None:
                continue
            suffix = raw[len(cleaned) :]
            parts.append(text[last_end:start])
            parts.append(alias + suffix)
            last_end = end

        if not parts:
            return text
        parts.append(text[last_end:])
        return "".join(parts)

    @staticmethod
    def _replace_legacy_image_hash_aliases(
        text: str,
        *,
        hash_to_alias: dict[str, str] | None = None,
    ) -> str:
        if not text:
            return text
        generated_aliases: dict[str, str] = {}
        next_index = 1

        def _replace(match: re.Match[str]) -> str:
            nonlocal next_index
            image_hash = match.group(1).lower()
            if hash_to_alias and image_hash in hash_to_alias:
                return hash_to_alias[image_hash]
            alias = generated_aliases.get(image_hash)
            if alias is None:
                alias = f"[image-{next_index}]"
                generated_aliases[image_hash] = alias
                next_index += 1
            return alias

        return LEGACY_IMAGE_SHA256_PATTERN.sub(_replace, text)

    @staticmethod
    def _replace_image_aliases_with_short_aliases(
        text: str,
        *,
        refs_by_index: dict[int, str],
        hash_to_alias: dict[str, str],
    ) -> str:
        if not text:
            return text

        def _replace(match: re.Match[str]) -> str:
            image_index = int(match.group(1))
            image_hash = refs_by_index.get(image_index, "").lower()
            if not image_hash:
                return match.group(0)
            return hash_to_alias.get(image_hash, match.group(0))

        return IMAGE_ALIAS_PATTERN.sub(_replace, text)

    @staticmethod
    def _extract_short_term_urls(messages: list[ShortTermMessage]) -> list[str]:
        seen: set[str] = set()
        urls: list[str] = []
        for item in messages:
            if item.role != "user":
                continue
            for match in URL_PATTERN.finditer(item.content):
                normalized = KisaragiAgent._normalize_url_from_match(match.group(1))
                if not normalized:
                    continue
                if normalized in seen:
                    continue
                seen.add(normalized)
                urls.append(normalized)
        return urls

    @staticmethod
    def _format_image_alias_text(image_aliases: list[str]) -> str:
        if not image_aliases:
            return "(none)"
        return ", ".join(image_aliases)

    @staticmethod
    def _message_to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict):
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
                elif isinstance(part, str):
                    parts.append(part)
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def _parse_memory_json(text: str) -> dict[str, Any]:
        content = text.strip()
        if not content:
            return {}

        if content.startswith("```"):
            content = content.strip("`")
            if content.lower().startswith("json"):
                content = content[4:].strip()

        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict):
                    return parsed
            except json.JSONDecodeError:
                return {}
        return {}

    @staticmethod
    def _normalize_memory_text(value: Any, fallback: Any = "") -> str:
        if isinstance(value, str):
            normalized = value.strip()
            if normalized:
                return normalized
        elif value is not None:
            try:
                serialized = json.dumps(value, ensure_ascii=False)
                if serialized.strip():
                    return serialized
            except Exception:
                pass

        if isinstance(fallback, str):
            return fallback
        if fallback is None:
            return ""
        try:
            return json.dumps(fallback, ensure_ascii=False)
        except Exception:
            return str(fallback)

    @staticmethod
    def _set_attachment(state: AgentState, step: str, value: str) -> dict[str, str]:
        merged = dict(state.get("step_attachments", {}))
        merged[step] = value
        return merged
