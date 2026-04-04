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
            storage_message="message: 请看这个链接 https://example.com ，并结合图片给出建议\n",
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

## 输入消息与持久化

- `ConversationRequest.message` 是实际发给 LLM 的文本。
- `ConversationRequest.storage_message` 可选；若提供，`memory` 步骤会把它而不是 `message` 写入短期记忆。
- OneBot 插件会用这个字段保证“发给 LLM 的简化文本”和“数据库里保存的 YAML”彼此独立。

## 轻量回复模型

- `step_models.lite_reply` 用于 `lite_chat` 路径的回复模型。
- 未配置 `lite_reply` 时，会自动回退到 `step_models.reply`，保持向后兼容。
- `reply` 与 `reply_lite` 现在分别带有“技术路径/休闲路径只处理对应消息，其余输入由其他路径处理”的约束；如果某条路径筛完后没有该它处理的内容，会输出 `bot选择沉默`，并且不会产生输出事件。
- `lite_chat` 路径会最多执行 3 轮 `reply_lite -> reply_lite_check`。检查失败时，会把所有评语追加到上一版回复末尾，再要求 `reply_lite` 生成新回复；第 3 次仍不通过则取消该路径回复。
- `reply_lite_check` 的评语是编译器风格的诊断文本：先指出错误位置，再引用 prompt 中对应规则的原文。当前检查器会忽略句首语气词（`哈*`、`啊`、`诶`、`哎`、`好家伙`、`呜*`、`前辈`）及其后的 `，！。？`，然后检查回复是否以“这”开头；还会用黑名单关键词拦截括号内容，当前关键词包括 `拍`、`递`、`捂`、`擦`、`晃`、`敲`、`挥`、`躲`、`低头`、`抬头`、`歪头`、`困惑`、`无辜`、`心虚`、`委屈`、`肩`、`脸`、`嘴`、`胸口`、`桌`、`手`、`认错`、`叹气`，以及 `拍肩`、`递零食`、`递奶茶`、`递咖啡`、`困惑脸`、`捂脸`、`小声`。
- 此外会直接拦截句尾括号表达：只要 `（...）` / `(...)` 落在行尾或文本末尾就判违规；如有误报，请在括号后补句号或其他标点。
- 技术路径覆盖技术提问、技术文章分享、技术讨论、事实求证、需要工具或分析的内容；技术路径回复要求输出不超过 150 字的技术性内容。

## 多路径回复

- `route` 节点现在输出路由数组，允许一轮消息同时命中 `default` 与 `lite_chat`。
- 每条路径会独立执行各自的中段图，并按路由数组顺序产出输出事件。
- 共享记忆收尾会等所有路径都结束后再执行，只把实际成功发送的路径回复一起写入记忆；`reply_lite` 的中间草稿和检查评语不会进入短期记忆。

## 调试日志

默认日志器名：`kisaragirin.agent`。
- `reply_lite_check` 会额外输出 `LITE-CHECK` 信息日志，记录每轮检查的 attempt、检查器名和失败评语。


## 开发文档

- 图与节点开发指南：GRAPH_DEVELOPMENT.md
