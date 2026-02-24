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
from importlib import import_module
from pathlib import Path
from threading import Lock, Thread
from typing import Any, TypedDict
from urllib.parse import unquote_to_bytes, urlsplit

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from .config import AgentConfig, ConversationRequest, ConversationResponse, ImageInput
from .memory import SQLiteMemoryStore, ShortTermMessage
from .prompts import (
    MEMORY_JSON_INSTRUCTION,
    STEP_SYSTEM_INSTRUCTIONS,
    URL_SUMMARY_PROMPT_TEMPLATE,
    VISION_DESCRIPTION_PROMPT,
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
IMAGE_SHA256_PATTERN = re.compile(r"\[image-sha256:([0-9a-fA-F]{64})\]")

_CONVERSATION_LOCKS: dict[str, Lock] = {}
_CONVERSATION_LOCKS_GUARD = Lock()


class AgentState(TypedDict, total=False):
    conversation_id: str
    user_message: str
    user_message_normalized: str
    images: list[ImageInput]
    url_aliases: dict[str, str]
    image_aliases: list[str]
    image_hashes: list[str]
    short_term_url_aliases: dict[str, str]
    short_term_image_aliases: dict[str, str]
    debug: bool
    long_term_memory: str
    short_term_context: str
    working_text: str
    reply: str
    step_attachments: dict[str, str]
    step_durations_ms: dict[str, float]


class _BackgroundAsyncRunner:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._run_loop, name="kisaragirin-async-runner", daemon=True)
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
    _PERF_STEP_ORDER = ("STEP-0", "STEP-1", "STEP-2", "STEP-3", "STEP-4", "STEP-5")
    _PERF_STEP_LABELS = {
        "STEP-0": "prepare",
        "STEP-1": "urls",
        "STEP-2": "vision",
        "STEP-3": "tools",
        "STEP-4": "reply",
        "STEP-5": "memory",
    }

    def __init__(self, config: AgentConfig, tools: Sequence[BaseTool] | None = None) -> None:
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
        self._graph = self._build_graph()

    def run(self, request: ConversationRequest) -> ConversationResponse:
        conversation_lock = self._get_conversation_lock(request.conversation_id)
        with conversation_lock:
            initial_state = self._build_initial_state(request)
            started_at = time.perf_counter()
            result = self._graph.invoke(initial_state)
            total_ms = (time.perf_counter() - started_at) * 1000
            self._log_performance_report(
                conversation_id=request.conversation_id,
                step_durations_ms=result.get("step_durations_ms"),
                total_ms=total_ms,
            )

            return ConversationResponse(
                reply=str(result.get("reply", "")),
            )

    async def arun(self, request: ConversationRequest) -> ConversationResponse:
        # Keep event loop responsive in async callers by running sync graph in a worker thread.
        return await asyncio.to_thread(self.run, request)

    @staticmethod
    def _build_initial_state(request: ConversationRequest) -> AgentState:
        return {
            "conversation_id": request.conversation_id,
            "user_message": request.message,
            "images": list(request.images),
            "debug": request.debug,
            "step_attachments": {},
        }

    @staticmethod
    def _resolve_future(
        loop: asyncio.AbstractEventLoop,
        future: asyncio.Future[Any],
        *,
        result: Any | None = None,
        error: BaseException | None = None,
    ) -> None:
        def _set() -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
                return
            future.set_result(result)

        try:
            loop.call_soon_threadsafe(_set)
        except RuntimeError:
            # Event loop may already be closed during shutdown.
            return

    async def arun_reply_first(
        self, request: ConversationRequest
    ) -> tuple[ConversationResponse, asyncio.Future[None]]:
        loop = asyncio.get_running_loop()
        reply_future: asyncio.Future[ConversationResponse] = loop.create_future()
        done_future: asyncio.Future[None] = loop.create_future()
        worker = Thread(
            target=self._run_reply_first_worker,
            args=(request, loop, reply_future, done_future),
            name=f"kisaragirin-reply-first-{request.conversation_id}",
            daemon=True,
        )
        worker.start()
        response = await reply_future
        return response, done_future

    def _run_reply_first_worker(
        self,
        request: ConversationRequest,
        loop: asyncio.AbstractEventLoop,
        reply_future: asyncio.Future[ConversationResponse],
        done_future: asyncio.Future[None],
    ) -> None:
        conversation_lock = self._get_conversation_lock(request.conversation_id)
        try:
            with conversation_lock:
                state = self._build_initial_state(request)
                started_at = time.perf_counter()
                state = self._with_step_timing("STEP-0", self._step0_prepare)(state)
                state = self._with_step_timing("STEP-1", self._step1_urls)(state)
                state = self._with_step_timing("STEP-2", self._step2_vision)(state)
                state = self._with_step_timing("STEP-3", self._step3_tools)(state)
                state = self._with_step_timing("STEP-4", self._step4_reply)(state)

                reply = ConversationResponse(reply=str(state.get("reply", "")))
                self._resolve_future(loop, reply_future, result=reply)

                state = self._with_step_timing("STEP-5", self._step5_memory)(state)
                total_ms = (time.perf_counter() - started_at) * 1000
                self._log_performance_report(
                    conversation_id=request.conversation_id,
                    step_durations_ms=state.get("step_durations_ms"),
                    total_ms=total_ms,
                )
        except Exception as exc:
            self._resolve_future(loop, reply_future, error=exc)
            self._resolve_future(loop, done_future, error=exc)
            return

        self._resolve_future(loop, done_future, result=None)

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

    def _build_graph(self):
        graph = StateGraph(AgentState)
        graph.add_node("step0_prepare", self._with_step_timing("STEP-0", self._step0_prepare))
        graph.add_node("step1_urls", self._with_step_timing("STEP-1", self._step1_urls))
        graph.add_node("step2_vision", self._with_step_timing("STEP-2", self._step2_vision))
        graph.add_node("step3_tools", self._with_step_timing("STEP-3", self._step3_tools))
        graph.add_node("step4_reply", self._with_step_timing("STEP-4", self._step4_reply))
        graph.add_node("step5_memory", self._with_step_timing("STEP-5", self._step5_memory))

        graph.add_edge(START, "step0_prepare")
        graph.add_edge("step0_prepare", "step1_urls")
        graph.add_edge("step1_urls", "step2_vision")
        graph.add_edge("step2_vision", "step3_tools")
        graph.add_edge("step3_tools", "step4_reply")
        graph.add_edge("step4_reply", "step5_memory")
        graph.add_edge("step5_memory", END)
        return graph.compile()

    def _with_step_timing(
        self,
        step_name: str,
        step_fn: Any,
    ):
        def _wrapped(state: AgentState) -> AgentState:
            started_at = time.perf_counter()
            result = step_fn(state)
            elapsed_ms = (time.perf_counter() - started_at) * 1000

            merged_step_durations: dict[str, float] = {}
            for source in (state.get("step_durations_ms"), result.get("step_durations_ms")):
                if isinstance(source, dict):
                    for key, value in source.items():
                        try:
                            merged_step_durations[str(key)] = float(value)
                        except Exception:
                            continue
            merged_step_durations[step_name] = elapsed_ms

            merged_result = dict(result)
            merged_result["step_durations_ms"] = merged_step_durations
            return merged_result

        return _wrapped

    def _step0_prepare(self, state: AgentState) -> AgentState:
        conversation_id = state["conversation_id"]
        normalized_message, url_aliases = self._replace_urls_with_aliases(state["user_message"])
        image_aliases = [f"[image-{idx}]" for idx, _ in enumerate(state.get("images") or [], start=1)]
        image_hashes = [self._compute_image_sha256(image) for image in (state.get("images") or [])]
        long_term_memory = self._memory_store.get_long_term(conversation_id)
        short_term_messages = self._memory_store.get_short_term(
            conversation_id=conversation_id,
            turn_window=self._config.short_term_turn_window,
        )
        short_term_context = self._format_short_term_context(short_term_messages)
        short_term_urls = self._extract_short_term_urls(short_term_messages)
        short_term_image_hashes = self._extract_short_term_image_hashes(short_term_messages)
        short_term_url_aliases = {
            f"[short-url-{idx}]": url for idx, url in enumerate(short_term_urls, start=1)
        }
        short_term_image_aliases = {
            f"[short-image-{idx}]": image_hash
            for idx, image_hash in enumerate(short_term_image_hashes, start=1)
        }
        image_alias_text = self._format_image_alias_text(image_aliases, image_hashes)
        short_term_url_alias_text = ", ".join(short_term_url_aliases.keys()) or "(none)"
        short_term_image_alias_text = ", ".join(short_term_image_aliases.keys()) or "(none)"

        working_text = (
            "[STEP-0-LONG-TERM-MEMORY]\n"
            f"{long_term_memory or '(empty)'}\n\n"
            "[STEP-0-FIXED-MEMORY]\n"
            f"{self._config.prompts.fixed_memory or '(empty)'}\n\n"
            "[STEP-0-SHORT-TERM-CONTEXT]\n"
            f"{short_term_context}\n\n"
            "[STEP-0-RESOURCE-ALIASES]\n"
            f"urls: {', '.join(url_aliases.keys()) or '(none)'}\n"
            f"images: {image_alias_text}\n"
            f"short_term_urls: {short_term_url_alias_text}\n"
            f"short_term_images: {short_term_image_alias_text}\n\n"
            "[STEP-0-ORIGINAL-INPUT]\n"
            f"{normalized_message}"
        )
        attachment_text = (
            "[STEP-0-LONG-TERM-MEMORY]\n"
            f"{long_term_memory or '(empty)'}\n\n"
            "[STEP-0-SHORT-TERM-CONTEXT]\n"
            f"{short_term_context}\n\n"
            "[STEP-0-RESOURCE-ALIASES]\n"
            f"urls: {', '.join(url_aliases.keys()) or '(none)'}\n"
            f"images: {image_alias_text}\n"
            f"short_term_urls: {short_term_url_alias_text}\n"
            f"short_term_images: {short_term_image_alias_text}"
        )
        self._log_step_debug(state, "STEP-0", attachment_text)

        return {
            "user_message_normalized": normalized_message,
            "url_aliases": url_aliases,
            "image_aliases": image_aliases,
            "image_hashes": image_hashes,
            "short_term_url_aliases": short_term_url_aliases,
            "short_term_image_aliases": short_term_image_aliases,
            "long_term_memory": long_term_memory,
            "short_term_context": short_term_context,
            "working_text": working_text,
            "step_attachments": self._set_attachment(
                state,
                "STEP-0",
                attachment_text,
            ),
        }

    def _step1_urls(self, state: AgentState) -> AgentState:
        url_aliases = state.get("url_aliases") or {}
        short_term_url_aliases = state.get("short_term_url_aliases") or {}
        if not url_aliases and not short_term_url_aliases:
            appendix = "[STEP-1-URL-SUMMARIES]\n(no url detected)"
            self._log_step_debug(state, "STEP-1", appendix)
            return {
                "working_text": state["working_text"] + "\n\n" + appendix,
                "step_attachments": self._set_attachment(state, "STEP-1", appendix),
            }

        blocks: list[str] = ["[STEP-1-URL-SUMMARIES]"]
        summary_by_url: dict[str, str] = {}

        for idx, (alias, url) in enumerate(url_aliases.items(), start=1):
            summary, from_cache, crawled_chars = self._get_or_create_url_summary(
                alias=alias,
                url=url,
                summary_by_url=summary_by_url,
            )
            cache_status = "hit" if from_cache else "miss"
            blocks.append(
                f"{idx}. {alias}\n"
                f"[URL] {url}\n"
                f"[CACHE] {cache_status}\n"
                f"[CRAWLED-CONTENT-CHARS] {crawled_chars}\n"
                f"[SUMMARY]\n{summary}"
            )

        if short_term_url_aliases:
            blocks.append("[STEP-1-SHORT-TERM-URL-SUMMARIES]")
            for idx, (alias, url) in enumerate(short_term_url_aliases.items(), start=1):
                summary, from_cache, crawled_chars = self._get_or_create_url_summary(
                    alias=alias,
                    url=url,
                    summary_by_url=summary_by_url,
                )
                cache_status = "hit" if from_cache else "miss"
                blocks.append(
                    f"{idx}. {alias}\n"
                    f"[URL] {url}\n"
                    f"[CACHE] {cache_status}\n"
                    f"[CRAWLED-CONTENT-CHARS] {crawled_chars}\n"
                    f"[SUMMARY]\n{summary}"
                )

        appendix = "\n\n".join(blocks)
        self._log_step_debug(state, "STEP-1", appendix)
        return {
            "working_text": state["working_text"] + "\n\n" + appendix,
            "step_attachments": self._set_attachment(state, "STEP-1", appendix),
        }

    def _step2_vision(self, state: AgentState) -> AgentState:
        images = state.get("images") or []
        short_term_image_aliases = state.get("short_term_image_aliases") or {}
        if not images and not short_term_image_aliases:
            appendix = "[STEP-2-IMAGE-DESCRIPTIONS]\n(no image input)"
            self._log_step_debug(state, "STEP-2", appendix)
            return {
                "working_text": state["working_text"] + "\n\n" + appendix,
                "step_attachments": self._set_attachment(state, "STEP-2", appendix),
            }

        image_aliases = state.get("image_aliases") or []
        image_hashes = state.get("image_hashes") or []
        blocks: list[str] = ["[STEP-2-IMAGE-DESCRIPTIONS]"]
        description_by_hash: dict[str, str] = {}
        for idx, image in enumerate(images, start=1):
            image_hash = image_hashes[idx - 1] if idx - 1 < len(image_hashes) else ""
            description = self._get_or_create_image_description(
                image=image,
                image_hash=image_hash,
                description_by_hash=description_by_hash,
            )
            alias = image_aliases[idx - 1] if idx - 1 < len(image_aliases) else f"[image-{idx}]"
            blocks.append(f"{idx}. {alias} [sha256:{image_hash or 'unknown'}]\n{description}")

        if short_term_image_aliases:
            blocks.append("[STEP-2-SHORT-TERM-IMAGE-DESCRIPTIONS]")
            for idx, (alias, image_hash) in enumerate(short_term_image_aliases.items(), start=1):
                description = description_by_hash.get(image_hash)
                if description is None:
                    description = self._memory_store.get_image_description(image_hash)
                if description is None:
                    description = "(description cache miss)"
                blocks.append(f"{idx}. {alias} [sha256:{image_hash}]\n{description}")

        appendix = "\n\n".join(blocks)
        self._log_step_debug(state, "STEP-2", appendix)
        return {
            "working_text": state["working_text"] + "\n\n" + appendix,
            "step_attachments": self._set_attachment(state, "STEP-2", appendix),
        }

    def _step3_tools(self, state: AgentState) -> AgentState:
        tool_model = self._model(self._config.step_models.tool).bind_tools(self._tools)
        messages: list[Any] = [
            SystemMessage(content=self._system_prompt("tool")),
            HumanMessage(content=state["working_text"]),
        ]

        logs: list[str] = ["[STEP-3-TOOL-EXTRA-INFO]"]
        used_tool = False

        for round_idx in range(1, self._config.max_tool_rounds + 1):
            raw_ai_message = tool_model.invoke(messages)
            ai_message = raw_ai_message
            if isinstance(raw_ai_message, AIMessage):
                # Avoid replaying Responses API item references when store=False.
                ai_message = AIMessage(
                    content=self._message_to_text(raw_ai_message.content),
                    tool_calls=raw_ai_message.tool_calls,
                )
            messages.append(ai_message)

            tool_calls = ai_message.tool_calls if isinstance(ai_message, AIMessage) else []
            if not tool_calls:
                final_note = self._message_to_text(ai_message.content)
                if final_note.strip():
                    logs.append(f"[ROUND-{round_idx}-MODEL-NOTE]\n{final_note.strip()}")
                break

            used_tool = True
            for call_idx, tool_call in enumerate(tool_calls, start=1):
                tool_name = tool_call.get("name", "")
                tool_args = tool_call.get("args", {})
                tool_id = tool_call.get("id", f"round-{round_idx}-call-{call_idx}")

                tool_output = self._invoke_tool(tool_name, tool_args)
                if len(tool_output) > self._config.max_tool_output_chars:
                    tool_output = tool_output[: self._config.max_tool_output_chars] + "\n...<truncated>"

                logs.append(
                    f"[ROUND-{round_idx}-TOOL-{call_idx}] {tool_name}\n"
                    f"args={json.dumps(tool_args, ensure_ascii=False)}\n"
                    f"output:\n{tool_output}"
                )

                messages.append(
                    ToolMessage(content=tool_output, tool_call_id=tool_id, name=tool_name)
                )

        if not used_tool:
            logs.append("No tool was called.")

        appendix = "\n\n".join(logs)
        self._log_step_debug(state, "STEP-3", appendix)
        return {
            "working_text": state["working_text"] + "\n\n" + appendix,
            "step_attachments": self._set_attachment(state, "STEP-3", appendix),
        }

    def _step4_reply(self, state: AgentState) -> AgentState:
        model = self._model(self._config.step_models.reply)
        reply_msg = model.invoke(
            [
                SystemMessage(content=self._system_prompt("reply")),
                HumanMessage(content=state["working_text"]),
            ]
        )
        reply_text = self._message_to_text(reply_msg.content)
        attachment = "[STEP-4-REPLY]\n" + reply_text
        self._log_step_debug(state, "STEP-4", attachment)
        return {
            "reply": reply_text,
            "step_attachments": self._set_attachment(state, "STEP-4", attachment),
        }

    def _step5_memory(self, state: AgentState) -> AgentState:
        memory_model = self._model(self._config.step_models.memory)

        msg = memory_model.invoke(
            [
                SystemMessage(content=self._system_prompt("memory")),
                HumanMessage(
                    content=(
                        f"{MEMORY_JSON_INSTRUCTION}\n\n"
                        "[PREVIOUS-LONG-TERM-MEMORY]\n"
                        f"{state.get('long_term_memory') or '(empty)'}\n\n"
                        "[THIS-TURN-ENRICHED-INPUT]\n"
                        f"{state['working_text']}\n\n"
                        "[THIS-TURN-REPLY]\n"
                        f"{state.get('reply', '')}"
                    )
                ),
            ]
        )

        parsed = self._parse_memory_json(self._message_to_text(msg.content))
        new_long_term = self._normalize_memory_text(
            parsed.get("long_term_memory"),
            fallback=state.get("long_term_memory", ""),
        )
        memory_compacted = False
        if len(new_long_term) > 2000:
            compact_msg = memory_model.invoke(
                [
                    SystemMessage(content=self._system_prompt("memory")),
                    HumanMessage(
                        content=(
                            f"{MEMORY_JSON_INSTRUCTION}\n\n"
                            "你的记忆太长了，需要精简到2000字符以内。\n\n"
                            "[CURRENT-LONG-TERM-MEMORY]\n"
                            f"{new_long_term}"
                        )
                    ),
                ]
            )
            compact_parsed = self._parse_memory_json(self._message_to_text(compact_msg.content))
            new_long_term = self._normalize_memory_text(
                compact_parsed.get("long_term_memory"),
                fallback=new_long_term,
            )
            if len(new_long_term) > 2000:
                new_long_term = new_long_term[:2000]
            memory_compacted = True
        user_message_for_memory = self._build_short_term_user_message(state)
        self._memory_store.persist_turn(
            conversation_id=state["conversation_id"],
            long_term_memory=new_long_term,
            user_message=user_message_for_memory,
            assistant_reply=state.get("reply", ""),
        )

        attachment = (
            "[STEP-5-MEMORY-UPDATE]\n"
            "long_term_memory_updated=true\n"
            f"long_term_memory_compacted={'true' if memory_compacted else 'false'}\n"
            "short_term_memory_appended=user+assistant"
        )
        self._log_step_debug(
            state,
            "STEP-5",
            attachment + f"\nupdated_long_term_memory:\n{new_long_term}",
        )
        return {
            "long_term_memory": new_long_term,
            "step_attachments": self._set_attachment(state, "STEP-5", attachment),
        }

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

        page_text = self._crawl_url_text(url=url, max_chars=self._config.max_crawl_chars)
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
        try:
            image_url = image.to_model_url()
        except Exception as exc:
            return f"Image payload error: {exc}"

        msg = model.invoke(
            [
                SystemMessage(content=self._system_prompt("vision")),
                HumanMessage(
                    content=[
                        {
                            "type": "text",
                            "text": VISION_DESCRIPTION_PROMPT,
                        },
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ]
                ),
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
            async with crawler_cls() as crawler:
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
        self._log_info("[DEBUG][%s][conversation=%s]\n%s", step, conversation_id, content)

    def _log_performance_report(
        self,
        *,
        conversation_id: str,
        step_durations_ms: dict[str, float] | None,
        total_ms: float,
    ) -> None:
        durations = step_durations_ms if isinstance(step_durations_ms, dict) else {}
        parts: list[str] = []
        for step_name in self._PERF_STEP_ORDER:
            step_label = self._PERF_STEP_LABELS.get(step_name, step_name.lower())
            step_display_name = f"{step_name}({step_label})"
            value = durations.get(step_name)
            if isinstance(value, (int, float)):
                parts.append(f"{step_display_name}={float(value):.2f}ms")
            else:
                parts.append(f"{step_display_name}=n/a")
        self._log_info(
            "[PERF][conversation=%s] total=%.2fms %s",
            conversation_id,
            total_ms,
            " ".join(parts),
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
        raise ValueError(f"Unsupported provider '{provider}'. Supported providers: openai, siliconflow")

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
            sections.append(f"---\nThis is a scenario task evaluation project used to test the capabilities of LLM. After reading the task file, complete the corresponding tasks according to the instructions within, and output the final results.\n---TASK FILE CONTENT---\n\n[STEP-INSTRUCTION:{step}]\n{instruction}")
        if self._config.prompts.persona.strip():
            sections.append("---Additional Task Requirements---\n[OUTPUT_STYLE.PERSONA]\n" + self._config.prompts.persona.strip())
        if sections:
            return ("\n\n".join(sections))+"\n---\n"
        return f"You are in step '{step}'.\n---\n"

    def _build_short_term_user_message(self, state: AgentState) -> str:
        user_message = str(state.get("user_message", ""))
        image_aliases = state.get("image_aliases") or []
        image_hashes = state.get("image_hashes") or []
        for idx, alias in enumerate(image_aliases):
            image_hash = image_hashes[idx] if idx < len(image_hashes) else ""
            if image_hash:
                user_message = user_message.replace(alias, f"[image-sha256:{image_hash}]")
        return user_message

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
    def _format_short_term_context(messages: list[ShortTermMessage]) -> str:
        if not messages:
            return "(empty)"
        lines = []
        for idx, item in enumerate(messages, start=1):
            lines.append(f"{idx}. [{item.role}] {item.content}")
        return "\n".join(lines)

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
    def _extract_short_term_image_hashes(messages: list[ShortTermMessage]) -> list[str]:
        seen: set[str] = set()
        hashes: list[str] = []
        for item in messages:
            if item.role != "user":
                continue
            for match in IMAGE_SHA256_PATTERN.finditer(item.content):
                value = match.group(1).lower()
                if value in seen:
                    continue
                seen.add(value)
                hashes.append(value)
        return hashes

    @staticmethod
    def _format_image_alias_text(image_aliases: list[str], image_hashes: list[str]) -> str:
        if not image_aliases:
            return "(none)"
        parts: list[str] = []
        for idx, alias in enumerate(image_aliases):
            image_hash = image_hashes[idx] if idx < len(image_hashes) else ""
            if image_hash:
                parts.append(f"{alias}(sha256:{image_hash})")
            else:
                parts.append(f"{alias}(sha256:unknown)")
        return ", ".join(parts)

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
