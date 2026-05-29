from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping, Self, Sequence, TypedDict, Unpack

from .message_types import Message
from .openviking import OpenVikingConfig


MessageFormat = Literal["yaml", "simple", "simple-id"]


@dataclass(slots=True, frozen=True)
class ModelConfig:
    id: str
    base_url: str
    api_key: str
    model: str
    provider: str = "openai"
    temperature: float = 0.2
    timeout: float | None = 60.0
    extra_body: dict[str, object] | None = None
    client_kwargs: dict[str, object] = field(default_factory=dict)
    model_kwargs: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class StepModelIds:
    summarize: str
    vision: str
    tool: str
    reply: str
    memory: str
    route: str = ""
    lite_reply: str = ""


@dataclass(slots=True, frozen=True)
class StepFallbackPools:
    summarize: tuple[str, ...] = ()
    vision: tuple[str, ...] = ()
    tool: tuple[str, ...] = ()
    reply: tuple[str, ...] = ()
    memory: tuple[str, ...] = ()
    route: tuple[str, ...] = ()
    lite_reply: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class CrawlerConfig:
    headless: bool = False
    verbose: bool = True
    user_data_dir: str | None = None


@dataclass(slots=True)
class PromptConfig:
    persona: str = ""
    fixed_memory: str = ""


@dataclass(slots=True)
class ImageInput:
    url: str | None = None
    base64_data: str | None = None
    mime_type: str = "image/png"
    name: str | None = None
    animation_frames: list["ImageInput"] = field(default_factory=list)

    def to_model_url(self) -> str:
        if self.url:
            return self.url
        if self.base64_data:
            return f"data:{self.mime_type};base64,{self.base64_data}"
        raise ValueError("ImageInput requires either url or base64_data")


@dataclass(slots=True)
class AgentConfig:
    models: Mapping[str, ModelConfig]
    step_models: StepModelIds
    step_fallbacks: StepFallbackPools = field(default_factory=StepFallbackPools)
    max_retries: int = 3
    prompts: PromptConfig = field(default_factory=PromptConfig)
    openviking: OpenVikingConfig = field(default_factory=OpenVikingConfig)
    message_format: MessageFormat = "yaml"
    self_name: str = "assistant"
    me_label: str = "(me)"
    exa_api_key: str = ""
    brave_search_api_key: str = ""
    serpapi_api_key: str = ""
    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    memory_db_path: str = ".kisaragirin_memory.sqlite3"
    short_term_turn_window: int = 12
    max_tool_rounds: int = 4
    max_crawl_chars: int = 64_000
    max_summary_chars: int = 5_000
    max_tool_output_chars: int = 64_000

    @classmethod
    def from_model_list(
        cls,
        models: Sequence[ModelConfig],
        step_models: StepModelIds,
        step_fallbacks: StepFallbackPools | None = None,
        max_retries: int = 3,
        prompts: PromptConfig | None = None,
        **kwargs: Unpack[AgentConfigKwargs],
    ) -> Self:
        model_map = {m.id: m for m in models}
        return cls(
            models=model_map,
            step_models=step_models,
            step_fallbacks=step_fallbacks or StepFallbackPools(),
            max_retries=max_retries,
            prompts=prompts or PromptConfig(),
            **kwargs,
        )


class AgentConfigKwargs(TypedDict, total=False):
    openviking: OpenVikingConfig
    message_format: MessageFormat
    self_name: str
    me_label: str
    exa_api_key: str
    brave_search_api_key: str
    serpapi_api_key: str
    crawler: CrawlerConfig
    memory_db_path: str
    short_term_turn_window: int
    max_tool_rounds: int
    max_crawl_chars: int
    max_summary_chars: int
    max_tool_output_chars: int
    step_fallbacks: StepFallbackPools
    max_retries: int


@dataclass(slots=True)
class ConversationRequest:
    conversation_id: str
    messages: list[Message] = field(default_factory=list)
    images: list[ImageInput] = field(default_factory=list)
    debug: bool = False


@dataclass(slots=True, frozen=True)
class OutputEvent:
    event_id: str
    event_type: str
    route_id: str
    content: str
    order: int
    final: bool = True
    dedupe_key: str = ""


@dataclass(slots=True)
class ConversationResponse:
    reply: str = ""
    outputs: list[OutputEvent] = field(default_factory=list)
    cancelled: bool = False
