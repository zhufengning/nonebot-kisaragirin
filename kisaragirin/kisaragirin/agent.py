from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Sequence
from importlib import import_module
from pathlib import Path
from threading import Lock, Thread
from typing import Annotated, Any, TypedDict
from urllib.parse import unquote_to_bytes, urlsplit

from crawl4ai import BrowserConfig
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import BaseTool

from .config import (
    AgentConfig,
    ConversationRequest,
    ConversationResponse,
    ImageInput,
    OutputEvent,
)
from .memory import ShortTermMessage, SQLiteMemoryStore
from .orchestration import StepImplementationRegistry
from .prompts import (
    ANIMATED_VISION_DESCRIPTION_PROMPT,
    STEP_SYSTEM_INSTRUCTIONS,
    URL_SUMMARY_PROMPT_TEMPLATE,
    VISION_DESCRIPTION_PROMPT,
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
    run_reply_lite,
)
from .steps_routing import run_route
from .orchestration import (
    build_graph_for_execution_plan,
    resolve_all_steps,
)
from .routing import (
    EMPTY_GRAPH,
    ExecutionPlan,
    RouteDecision,
    build_default_route_decision,
    build_route_selection_plan,
    build_execution_plan,
    normalize_route_ids,
)
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
        route_selection_graph = self._build_graph_for_execution_plan(route_selection_plan)
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
                finalize_graph = self._build_graph_for_execution_plan(handle.finalize_plan)
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
            if metric_name.startswith("reply") and "reply_completed_ms" not in merged_result:
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
    ) -> tuple[str, bool, int]:
        existing = summary_by_url.get(url)
        if existing is not None:
            return existing, True, 0

        cached = self._memory_store.get_url_summary(url)
        if cached is not None:
            summary_by_url[url] = cached
            return cached, True, 0

        page_text = self._crawl_url_text(
            url=url, max_chars=self._config.max_crawl_chars
        )
        summary = self._summarize_url(alias=alias, page_text=page_text)
        self._memory_store.set_url_summary(url, summary)
        summary_by_url[url] = summary
        return summary, False, len(page_text)

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

    def _log_performance_report(
        self,
        *,
        conversation_id: str,
        step_durations_ms: dict[str, float] | None,
        reply_completed_ms: float | None,
        total_ms: float,
    ) -> None:
        durations = step_durations_ms if isinstance(step_durations_ms, dict) else {}
        parts: list[str] = []
        for step_name, value in durations.items():
            if isinstance(value, (int, float)):
                parts.append(f"{step_name}={float(value):.2f}ms")
        metrics: list[str] = []
        if isinstance(reply_completed_ms, (int, float)):
            metrics.append(f"reply_total={float(reply_completed_ms):.2f}ms")
        metrics.append(f"total={total_ms:.2f}ms")
        metrics.extend(parts)
        self._log_info(
            "[PERF][conversation=%s] %s",
            conversation_id,
            " ".join(metrics),
        )

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
                alias = f"[url-{next_index}]"
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
        if (
            step not in {"summarize", "vision"}
            and self._config.prompts.persona.strip()
        ):
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
        short_term_image_refs: dict[float, dict[int, str]] | None = None,
        short_term_hash_to_alias: dict[str, str] | None = None,
        short_term_url_to_alias: dict[str, str] | None = None,
    ) -> str:
        if not messages:
            return "(empty)"
        lines = []
        for idx, item in enumerate(messages, start=1):
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
            lines.append(f"{idx}. [{item.role}] {content}")
        return "\n".join(lines)

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
