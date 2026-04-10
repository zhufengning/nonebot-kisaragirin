from __future__ import annotations

from kisaragirin import CrawlerConfig, ModelConfig, OpenVikingConfig, StepModelIds

from zfnbot.plugins.kisaragirin_onebot.config_schema import (
    GroupConfig,
    PluginConfig,
)


PLUGIN_CONFIG = PluginConfig(
    message_format="yaml",
    short_term_turn_window=12,
    image_max_upload_bytes=4 * 1024 * 1024,
    debug=True,
    exa_api_key="",
    brave_search_api_key="",
    serpapi_api_key="",
    openviking=OpenVikingConfig(
        enabled=True,
        mode="http",
        url="http://localhost:1933",
        root_api_key="",
        account="kisaragirin",
        conversation_user_prefix="onebot-group-",
        agent_id="kisaragirin-onebot",
        search_target_uri="viking://user/memories/",
        search_limit=5,
    ),
    crawler=CrawlerConfig(
        headless=False,
        verbose=True,
        user_data_dir=None,
    ),
    models=(
        ModelConfig(
            id="lite",
            provider="openai",
            base_url="https://api.siliconflow.cn/v1",
            api_key="sk-",
            model="Qwen/Qwen3-8B",
            extra_body={"enable_thinking": False},
        ),
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
            extra_body={"enable_thinking": False},
        ),
    ),
    step_models=StepModelIds(
        summarize="kimi",
        vision="vision",
        tool="kimi",
        route="lite",
        lite_reply="lite",
        reply="kimi",
        memory="kimi",
    ),
    ops=(123456789,),
    groups={
        1234567890: GroupConfig(
            persona="""你是一只猫娘""",
            fixed_memory="""这里是一些固定的记忆，会被注入到对话上下文中""",
        ),
    },
)
