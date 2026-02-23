# kisaragirin

一个基于 LangGraph 的 Python Agent 包，面向“被其他 Python 代码调用”的场景。

## 特性

- 多模型配置：每个模型有独立 `id/provider/base_url/api_key/model`
- 每个步骤按 `id` 选择模型，可重复复用同一模型配置
- `crawl4ai` 为必选依赖（URL 抓取步骤强依赖）
- 内置短期记忆（上下文）与长期记忆（持久化到 SQLite）
- 内置工具：`read_url`、`exa_search`（Exa，可选）、`web_search`（Exa/Brave，可选）、`scholar_search`（SerpApi，可选）
- 同一 `conversation_id` 在进程内串行执行，避免并发读写导致记忆错乱
- 各步骤指令提示词由包内固定，不对调用者暴露修改入口
- 处理流程固定为：
  1. 注入长期记忆与短期上下文
  2. 提取 URL，用 crawl4ai 抓取并由 summarize 模型总结
  3. 用 vision 模型将输入图片替换为文字描述
  4. 用 tool 模型判定并执行多轮工具调用，追加外部信息
  5. 用 reply 模型生成最终回复
  6. 用 memory 模型更新记忆并返回结果

## 快速使用

```python
from kisaragirin import (
    AgentConfig,
    ConversationRequest,
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
    ModelConfig(
        id="gpt4o-mini",
        provider="openai",
        base_url="https://api.openai.com/v1",
        api_key="YOUR_KEY",
        model="gpt-4o-mini",
    ),
    ModelConfig(
        id="sf-reasoner",
        provider="siliconflow",
        base_url="https://api.siliconflow.cn/v1",
        api_key="YOUR_SILICONFLOW_KEY",
        model="Qwen/Qwen3-14B",
        # 非 OpenAI 标准参数请放到 extra_body，避免
        # TypeError: Completions.create() got an unexpected keyword argument ...
        extra_body={"thinking_budget": 1024},
        # 需要直接传给 Chat* 构造器的参数可放在 client_kwargs
        # client_kwargs={"use_responses_api": True},
    ),
]

config = AgentConfig.from_model_list(
    models=models,
    step_models=StepModelIds(
        summarize="gpt4o-mini",
        vision="gpt4o",
        tool="gpt4o-mini",
        reply="gpt4o",
        memory="gpt4o-mini",
    ),
    prompts=PromptConfig(persona="你是一个专业且可靠的助手。"),
    exa_api_key="YOUR_EXA_API_KEY",   # 可选，启用 Exa web_search
    brave_search_api_key="",          # 可选，当 exa_api_key 为空时可用 Brave 回退
    serpapi_api_key="",               # 可选，启用 scholar_search
)

with KisaragiAgent(config) as agent:
    response = agent.run(
        ConversationRequest(
            conversation_id="conv-001",
            message="请看这个链接 https://example.com ，并结合图片给出建议",
            debug=False,  # 为 true 时，调试信息输出到日志（logger: kisaragirin.agent）
        )
    )
    print(response.reply)
```

异步场景可直接调用：

```python
response = await agent.arun(request)
```

## 返回值

`ConversationResponse` 包含：

- `reply`: 最终回复
- 不包含调试字段；调试内容在 `ConversationRequest.debug=True` 时按步骤实时输出到日志

## 调试日志

默认日志器名：`kisaragirin.agent`。示例：

```python
import logging

logging.basicConfig(level=logging.INFO)
```
