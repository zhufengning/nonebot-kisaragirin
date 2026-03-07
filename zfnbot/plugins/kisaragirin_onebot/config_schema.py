from __future__ import annotations

from dataclasses import dataclass, field

from kisaragirin import CrawlerConfig, ModelConfig, StepModelIds


@dataclass(slots=True, frozen=True)
class GroupConfig:
    persona: str
    fixed_memory: str = ""


@dataclass(slots=True, frozen=True)
class ReplyTimingConfig:
    mention_quiet_seconds: int = 8
    idle_start_minutes: int = 5
    idle_expect_minutes: int = 15


@dataclass(slots=True, frozen=True, kw_only=True)
class PluginConfig:
    models: tuple[ModelConfig, ...]
    step_models: StepModelIds
    groups: dict[int, GroupConfig]
    short_term_turn_window: int = 12
    ops: tuple[int, ...] = ()
    exa_api_key: str = ""
    brave_search_api_key: str = ""
    serpapi_api_key: str = ""
    timing: ReplyTimingConfig = field(default_factory=ReplyTimingConfig)
    crawler: CrawlerConfig = field(default_factory=CrawlerConfig)
    memory_db_path: str = ".kisaragirin_memory.sqlite3"
    debug: bool = False
