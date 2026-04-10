from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol, Sequence, TypeVar, cast


_T = TypeVar("_T")


class _AsyncRunnerProtocol(Protocol):
    def run(self, coro: Awaitable[_T]) -> _T: ...


class _OpenVikingContextProtocol(Protocol):
    uri: str
    abstract: str
    overview: str
    content: str
    context_type: str


class _OpenVikingSearchResultProtocol(Protocol):
    memories: Sequence[_OpenVikingContextProtocol]


class _OpenVikingSessionProtocol(Protocol):
    def add_message(self, *, role: str, parts: list[dict[str, Any]]) -> Any: ...
    def commit(self) -> Any: ...


class _OpenVikingClientProtocol(Protocol):
    def initialize(self) -> Any: ...
    def close(self) -> Any: ...
    def session(self, *, session_id: str) -> _OpenVikingSessionProtocol | Any: ...
    def search(
        self,
        *,
        query: str,
        session: _OpenVikingSessionProtocol,
        target_uri: str,
        limit: int,
    ) -> _OpenVikingSearchResultProtocol | Any: ...


@dataclass(slots=True, frozen=True)
class OpenVikingConfig:
    enabled: bool = False
    mode: Literal["embedded", "http"] = "http"
    path: str = ".openviking"
    url: str = "http://localhost:1933"
    api_key: str = ""
    agent_id: str = "kisaragirin"
    session_prefix: str = ""
    search_target_uri: str = "viking://user/memories/"
    search_limit: int = 5


@dataclass(slots=True, frozen=True)
class OpenVikingToolEvent:
    tool_name: str
    tool_input: Any
    tool_output: str
    success: bool = True


class OpenVikingBridge:
    def __init__(
        self,
        config: OpenVikingConfig,
        *,
        base_dir: Path,
        logger: logging.Logger,
        async_runner: _AsyncRunnerProtocol,
    ) -> None:
        self._config = config
        self._base_dir = base_dir
        self._logger = logger
        self._async_runner = async_runner
        self._client: _OpenVikingClientProtocol | None = None
        self._enabled = bool(config.enabled)
        if not self._enabled:
            return
        self._initialize_client()

    @property
    def enabled(self) -> bool:
        return self._enabled and self._client is not None

    def close(self) -> None:
        if self._client is None:
            return
        try:
            self._run_maybe_awaitable(self._client.close())
        except Exception as exc:
            self._logger.warning("OpenViking close failed: %s", exc)
        finally:
            self._client = None

    def search_memories(self, conversation_id: str, query: str) -> str:
        if not self.enabled:
            return ""
        client = self._client
        if client is None:
            return ""
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return "[OPENVIKING-MEMORY]\n(empty)"
        try:
            session = self._session(conversation_id)
            result = self._run_maybe_awaitable(
                client.search(
                    query=normalized_query,
                    session=session,
                    target_uri=self._config.search_target_uri,
                    limit=max(1, int(self._config.search_limit)),
                )
            )
            contexts = self._extract_contexts(result)
        except Exception as exc:
            self._logger.warning(
                "OpenViking search failed for conversation %s: %s",
                conversation_id,
                exc,
            )
            return "[OPENVIKING-MEMORY]\n(search failed)"

        if not contexts:
            return "[OPENVIKING-MEMORY]\n(empty)"

        blocks = ["[OPENVIKING-MEMORY]"]
        for index, context in enumerate(contexts, start=1):
            uri = str(getattr(context, "uri", "") or "").strip()
            abstract = self._normalize_text(
                getattr(context, "abstract", None)
                or getattr(context, "overview", None)
                or getattr(context, "content", None)
                or ""
            )
            if not abstract:
                continue
            header = f"{index}. {uri}" if uri else f"{index}."
            blocks.append(f"{header}\n{abstract}")

        if len(blocks) == 1:
            blocks.append("(empty)")
        return "\n\n".join(blocks)

    def commit_turn(
        self,
        *,
        conversation_id: str,
        user_message: str,
        assistant_reply: str,
        tool_events: list[OpenVikingToolEvent],
    ) -> dict[str, Any]:
        if not self.enabled:
            return {"status": "disabled"}
        if self._client is None:
            return {"status": "disabled"}

        session = self._session(conversation_id)
        user_parts = [self._build_text_part(str(user_message or ""))]
        assistant_text = str(assistant_reply or "").strip()
        tool_text = self._render_tool_events(tool_events)
        if tool_text:
            assistant_text = (
                f"{assistant_text}\n\n{tool_text}".strip()
                if assistant_text
                else tool_text
            )
        assistant_parts = [self._build_text_part(assistant_text)]

        self._run_maybe_awaitable(
            session.add_message(role="user", parts=user_parts)
        )
        self._run_maybe_awaitable(
            session.add_message(role="assistant", parts=assistant_parts)
        )
        result = self._run_maybe_awaitable(session.commit())
        if isinstance(result, dict):
            return result
        return {"status": "committed"}

    def _initialize_client(self) -> None:
        module = import_module("openviking")
        client_factory: Callable[..., _OpenVikingClientProtocol]
        client_kwargs: dict[str, Any]
        if self._config.mode == "embedded":
            client_factory = cast(
                Callable[..., _OpenVikingClientProtocol],
                getattr(module, "OpenViking"),
            )
            path = Path(self._config.path)
            if not path.is_absolute():
                path = (self._base_dir / path).resolve()
            client_kwargs = {"path": str(path)}
        else:
            client_factory = cast(
                Callable[..., _OpenVikingClientProtocol],
                getattr(module, "SyncHTTPClient"),
            )
            # Always pass explicit HTTP config here instead of relying on the SDK
            # to auto-load ~/.openviking/ovcli.conf. That file is for the CLI and
            # may contain fields unsupported by the Python client.
            client_kwargs = {
                "url": self._config.url,
                "api_key": self._config.api_key,
                "agent_id": self._config.agent_id,
            }
        client = client_factory(**client_kwargs)
        self._run_maybe_awaitable(client.initialize())
        self._client = client

    def _session(self, conversation_id: str) -> _OpenVikingSessionProtocol:
        if self._client is None:
            raise RuntimeError("OpenViking client is not initialized")
        return cast(
            _OpenVikingSessionProtocol,
            self._run_maybe_awaitable(
                self._client.session(
                    session_id=f"{self._config.session_prefix}{conversation_id}"
                )
            )
        )

    def _run_maybe_awaitable(self, value: _T | Awaitable[_T]) -> _T:
        if inspect.isawaitable(value):
            return self._async_runner.run(value)
        return cast(_T, value)

    def _build_text_part(self, text: str) -> Any:
        normalized_text = str(text or "").strip()
        try:
            message_module = import_module("openviking.message")
            text_part_cls = getattr(message_module, "TextPart")
            return text_part_cls(text=normalized_text)
        except Exception:
            return {"type": "text", "text": normalized_text}

    @staticmethod
    def _render_tool_events(tool_events: Sequence[OpenVikingToolEvent]) -> str:
        if not tool_events:
            return ""
        blocks = ["[TOOL-RESULTS]"]
        for index, event in enumerate(tool_events, start=1):
            blocks.append(
                f"{index}. {event.tool_name}\n"
                f"success={'true' if event.success else 'false'}\n"
                f"input={json.dumps(OpenVikingBridge._json_ready(event.tool_input), ensure_ascii=False)}\n"
                f"output:\n{event.tool_output}"
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _extract_contexts(result: Any) -> list[_OpenVikingContextProtocol]:
        memories = getattr(result, "memories", None)
        if isinstance(memories, list):
            return cast(list[_OpenVikingContextProtocol], memories)
        if memories:
            return cast(list[_OpenVikingContextProtocol], list(memories))
        if result is None:
            return []
        if isinstance(result, list):
            return cast(list[_OpenVikingContextProtocol], result)
        try:
            items = list(result)
        except TypeError:
            return []
        return cast(
            list[_OpenVikingContextProtocol],
            [
            item
            for item in items
            if str(getattr(item, "context_type", "") or "").strip().lower() == "memory"
            ],
        )

    @staticmethod
    def _json_ready(value: Any) -> Any:
        try:
            return json.loads(json.dumps(value, ensure_ascii=False))
        except Exception:
            return str(value)

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if value is None:
            return ""
        return str(value).strip()
