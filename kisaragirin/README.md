# kisaragirin

一个基于 LangGraph 的 Python Agent 包，面向“被其他 Python 代码调用”的场景。

## 特性

- 多模型配置：每个模型有独立 `id/provider/base_url/api_key/model`
- 每个步骤按 `id` 选择模型，可重复复用同一模型配置
- crawler 运行参数可配置：`headless`、`verbose`、`user_data_dir`
- `crawl4ai` 为必选依赖（URL 抓取步骤强依赖）
- 内置短期记忆（上下文）与长期记忆（持久化到 SQLite）
- 内置工具：`read_url`、`exa_search`（Exa，可选）、`web_search`（Exa/Brave，可选）、`scholar_search`（SerpApi，可选）
- 同一 `conversation_id` 在进程内串行执行，避免并发读写导致记忆错乱
- 各步骤指令提示词由包内固定，不对调用者暴露修改入口

## 快速使用

```python
from kisaragirin import (
    AgentConfig,
    ConversationRequest,
    CrawlerConfig,
    KisaragiAgent,
    ModelConfig,
    PromptConfig,
    StepModelIds,
)

models = [
    ModelConfig(
        id="gpt4o",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key="YOUR_KEY",
        model="gpt-4o",
    ),
]

config = AgentConfig.from_model_list(
    models=models,
    step_models=StepModelIds(
        summarize="gpt4o",
        vision="gpt4o",
        tool="gpt4o",
        reply="gpt4o",
        memory="gpt4o",
        lite_reply="gpt4o-mini",
    ),
    prompts=PromptConfig(persona="你是一个专业且可靠的助手。"),
    crawler=CrawlerConfig(
        headless=False,
        verbose=True,
        user_data_dir=None,
    ),
)

with KisaragiAgent(config) as agent:
    response = agent.run(
        ConversationRequest(
            conversation_id="conv-001",
            message="请看这个链接 https://example.com ，并结合图片给出建议",
            debug=False,
        )
    )
    print(response.reply)
```

## 返回值

`ConversationResponse` 目前包含：

- `reply`：最终回复

## 轻量回复模型

- `step_models.lite_reply` 用于 `lite_chat` 路径的回复模型。
- 未配置 `lite_reply` 时，会自动回退到 `step_models.reply`，保持向后兼容。
- `reply` 与 `reply_lite` 现在分别使用各自的系统提示词；当前 `reply_lite` 先复制了一份与 `reply` 相同的提示词，后续可独立调整。

## 调试日志

默认日志器名：`kisaragirin.agent`。


## 开发文档

- 图与节点开发指南：GRAPH_DEVELOPMENT.md


