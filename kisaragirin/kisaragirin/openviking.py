from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol, Sequence, TypeVar, cast
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from .memory import SQLiteMemoryStore


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
    root_api_key: str = ""
    account: str = ""
    user: str = ""
    agent_id: str = "kisaragirin"
    session_prefix: str = ""
    conversation_user_prefix: str = ""
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
        memory_store: SQLiteMemoryStore,
    ) -> None:
        self._config = config
        self._base_dir = base_dir
        self._logger = logger
        self._async_runner = async_runner
        self._memory_store = memory_store
        self._client: _OpenVikingClientProtocol | None = None
        self._conversation_clients: dict[str, _OpenVikingClientProtocol] = {}
        self._enabled = bool(config.enabled)
        if not self._enabled:
            return
        if not self._uses_conversation_user_keys:
            self._client = self._build_client(
                api_key=self._static_api_key,
                account=self._config.account,
                user=self._config.user,
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def close(self) -> None:
        clients = list(self._conversation_clients.values())
        if self._client is not None:
            clients.append(self._client)
        self._conversation_clients.clear()
        self._client = None
        for client in clients:
            try:
                self._run_maybe_awaitable(client.close())
            except Exception as exc:
                self._logger.warning("OpenViking close failed: %s", exc)

    def search_memories(self, conversation_id: str, query: str) -> str:
        if not self.enabled:
            return ""
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return "[OPENVIKING-MEMORY]\n(empty)"
        try:
            client = self._client_for_conversation(conversation_id)
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

    @property
    def _uses_conversation_user_keys(self) -> bool:
        return (
            self._config.mode == "http"
            and not self._static_api_key
            and bool(str(self._config.root_api_key).strip())
            and bool(str(self._config.account).strip())
            and bool(str(self._config.conversation_user_prefix).strip())
        )

    @property
    def _static_api_key(self) -> str:
        return str(self._config.api_key or "").strip()

    def _build_client(
        self,
        *,
        api_key: str,
        account: str = "",
        user: str = "",
    ) -> _OpenVikingClientProtocol:
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
                "api_key": api_key,
                "agent_id": self._config.agent_id,
            }
            if account:
                client_kwargs["account"] = account
            if user:
                client_kwargs["user"] = user
        client = client_factory(**client_kwargs)
        self._run_maybe_awaitable(client.initialize())
        return client

    def _client_for_conversation(
        self, conversation_id: str
    ) -> _OpenVikingClientProtocol:
        if not self._uses_conversation_user_keys:
            if self._client is None:
                raise RuntimeError("OpenViking client is not initialized")
            return self._client

        client = self._conversation_clients.get(conversation_id)
        if client is not None:
            return client

        account_id, user_id, user_key = self._get_or_create_conversation_user_key(
            conversation_id
        )
        client = self._build_client(
            api_key=user_key,
            account=account_id,
            user=user_id,
        )
        self._conversation_clients[conversation_id] = client
        return client

    def _session(self, conversation_id: str) -> _OpenVikingSessionProtocol:
        client = self._client_for_conversation(conversation_id)
        return cast(
            _OpenVikingSessionProtocol,
            self._run_maybe_awaitable(
                client.session(
                    session_id=f"{self._config.session_prefix}{conversation_id}"
                )
            )
        )

    def _get_or_create_conversation_user_key(
        self, conversation_id: str
    ) -> tuple[str, str, str]:
        account_id = str(self._config.account or "").strip()
        user_id = self._conversation_user_id(conversation_id)
        cached = self._memory_store.get_openviking_user_key(conversation_id)
        if cached is not None:
            cached_account_id, cached_user_id, cached_user_key = cached
            if (
                cached_account_id == account_id
                and cached_user_id == user_id
                and str(cached_user_key).strip()
            ):
                return cached_account_id, cached_user_id, cached_user_key

        user_key = self._create_or_regenerate_user_key(account_id, user_id)
        self._memory_store.set_openviking_user_key(
            conversation_id,
            account_id=account_id,
            user_id=user_id,
            user_key=user_key,
        )
        return account_id, user_id, user_key

    def _conversation_user_id(self, conversation_id: str) -> str:
        normalized_id = re.sub(r"[^0-9A-Za-z._-]+", "-", str(conversation_id)).strip(
            "-"
        )
        if not normalized_id:
            normalized_id = "conversation"
        return f"{self._config.conversation_user_prefix}{normalized_id}"

    def _create_or_regenerate_user_key(self, account_id: str, user_id: str) -> str:
        create_payload = {"user_id": user_id, "role": "user"}
        response = self._admin_api_request(
            method="POST",
            path=f"/api/v1/admin/accounts/{urllib_parse.quote(account_id)}/users",
            payload=create_payload,
            allow_statuses={409},
        )
        if response["status"] != 409:
            return self._extract_user_key(response["body"], action="create user")

        self._logger.warning(
            "OpenViking user %s/%s already exists; regenerating key because SQLite cache is missing",
            account_id,
            user_id,
        )
        regenerate_response = self._admin_api_request(
            method="POST",
            path=(
                "/api/v1/admin/accounts/"
                f"{urllib_parse.quote(account_id)}/users/{urllib_parse.quote(user_id)}/key"
            ),
            payload=None,
        )
        return self._extract_user_key(regenerate_response["body"], action="regenerate key")

    def _extract_user_key(self, body: Any, *, action: str) -> str:
        if not isinstance(body, dict):
            raise RuntimeError(f"OpenViking {action} returned non-JSON response")
        result = body.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"OpenViking {action} response missing result")
        user_key = str(result.get("user_key", "") or "").strip()
        if not user_key:
            raise RuntimeError(f"OpenViking {action} response missing user_key")
        return user_key

    def _admin_api_request(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        allow_statuses: set[int] | None = None,
    ) -> dict[str, Any]:
        root_api_key = str(self._config.root_api_key or "").strip()
        if not root_api_key:
            raise RuntimeError(
                "OpenViking root_api_key is required for user provisioning"
            )
        url = f"{self._config.url.rstrip('/')}{path}"
        body_bytes = None
        headers = {"X-API-Key": root_api_key}
        if payload is not None:
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib_request.Request(
            url=url,
            data=body_bytes,
            headers=headers,
            method=method,
        )
        try:
            with urllib_request.urlopen(request) as response:
                response_body = response.read().decode("utf-8")
                parsed_body = (
                    json.loads(response_body) if response_body.strip() else {}
                )
                return {"status": int(response.status), "body": parsed_body}
        except urllib_error.HTTPError as exc:
            response_body = exc.read().decode("utf-8")
            parsed_body: Any
            try:
                parsed_body = json.loads(response_body) if response_body.strip() else {}
            except Exception:
                parsed_body = {"error": response_body}
            if allow_statuses and exc.code in allow_statuses:
                return {"status": int(exc.code), "body": parsed_body}
            raise RuntimeError(
                f"OpenViking admin request failed: status={exc.code} body={parsed_body}"
            ) from exc
        except urllib_error.URLError as exc:
            raise RuntimeError(f"OpenViking admin request failed: {exc}") from exc

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
