from __future__ import annotations

from dataclasses import dataclass, field

from kisaragirin import ModelConfig, StepModelIds


@dataclass(slots=True, frozen=True)
class GroupConfig:
    persona: str


@dataclass(slots=True, frozen=True)
class ReplyTimingConfig:
    mention_quiet_seconds: int = 8
    idle_start_minutes: int = 5
    idle_expect_minutes: int = 15


@dataclass(slots=True, frozen=True)
class PluginConfig:
    models: tuple[ModelConfig, ...]
    step_models: StepModelIds
    brave_search_api_key: str = ""
    serpapi_api_key: str = ""
    groups: dict[int, GroupConfig]
    timing: ReplyTimingConfig = field(default_factory=ReplyTimingConfig)
    memory_db_path: str = ".kisaragirin_memory.sqlite3"
    debug: bool = False


PLUGIN_CONFIG = PluginConfig(
    debug=True,
    brave_search_api_key="",
    serpapi_api_key="",
    models=(
        ModelConfig(
            id="vision",
            provider="siliconflow",
            base_url="https://api.siliconflow.cn/v1",
            api_key="sk-",
            model="zai-org/GLM-4.6V",
        ),
        ModelConfig(
            id="kimi",
            provider="openai",
            base_url="https://api.siliconflow.cn/v1",
            api_key="sk-",
            model="Pro/moonshotai/Kimi-K2.5",
            extra_body={"enable_thinking": False}
        ),
    ),
    step_models=StepModelIds(
        summarize="kimi",
        vision="vision",
        tool="kimi",
        reply="kimi",
        memory="kimi",
    ),
    groups={
        1234567890: GroupConfig(persona="""你是一只猫娘"""),
    },
)
