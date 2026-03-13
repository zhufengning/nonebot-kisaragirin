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

- `reply`：把所有非沉默输出按顺序拼接后的文本
- `outputs`：输出事件列表；当前只会产生 `reply` 类型事件，包含 `route_id`、`content`、`order`、`event_id`
- `cancelled`：是否整轮都选择沉默（没有任何输出事件）

## 轻量回复模型

- `step_models.lite_reply` 用于 `lite_chat` 路径的回复模型。
- 未配置 `lite_reply` 时，会自动回退到 `step_models.reply`，保持向后兼容。
- `reply` 与 `reply_lite` 现在分别带有“技术路径/休闲路径只处理对应消息，其余输入由其他路径处理”的约束；如果某条路径筛完后没有该它处理的内容，会输出 `bot选择沉默`，并且不会产生输出事件。
- 技术路径覆盖技术提问、技术文章分享、技术讨论、事实求证、需要工具或分析的内容；技术路径回复要求输出不超过 150 字的技术性内容。

## 多路径回复

- `route` 节点现在输出路由数组，允许一轮消息同时命中 `default` 与 `lite_chat`。
- 每条路径会独立执行各自的中段图，并按路由数组顺序产出输出事件。
- 共享记忆收尾会等所有路径都结束后再执行，只把实际成功发送的路径回复一起写入记忆。

## 调试日志

默认日志器名：`kisaragirin.agent`。


## 开发文档

- 图与节点开发指南：GRAPH_DEVELOPMENT.md


