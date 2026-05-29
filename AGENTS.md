# bot_renew 项目信息

请在做出任何修改后检查是否需要更新README, AGENTS.md以及其他文档。

修改代码后必须使用ty check和ruff check检查并修复报错，该指令不属于运行测试，也不会调用项目中的任何脚本。禁止使用basedpyright(`uv run ty check`和`uv run ruff check`)

始终按最优方案编写代码，不要在乎任何兼容性。

## 目录索引

- `README.md`：项目入口说明、启动方式与文档导航。
- `TODO.md`：当前重构路线与阶段状态。
- `zfnbot/plugins/kisaragirin_onebot/README.md`：OneBot 插件行为、配置与调度说明。
- `kisaragirin/README.md`：Agent 包说明。
- `kisaragirin/GRAPH_DEVELOPMENT.md`：新增节点、建图、条件边、并行与 gate 设计指南。

## 项目概览

- 这是一个基于 NoneBot2 + OneBot V11 的群聊机器人项目。
- 主要逻辑由本地插件 `zfnbot/plugins/kisaragirin_onebot` 提供。
- 对话与工具调用核心由本地包 `kisaragirin` 提供（LangGraph 流程）。

## 代码结构

- `bot.py`：启动入口，注册 OneBot V11 适配器并加载 `zfnbot/plugins`。
- `zfnbot/plugins/kisaragirin_onebot/__init__.py`：插件入口，仅负责注册消息/指令处理器与关闭钩子。
- `zfnbot/plugins/kisaragirin_onebot/handlers.py`：群消息接入与入队入口。
- `zfnbot/plugins/kisaragirin_onebot/parser.py`：消息段解析、reply 递归加载、图片提取。
- `zfnbot/plugins/kisaragirin_onebot/scheduler.py`：队列触发策略、发送回复、worker 刷新。
- `zfnbot/plugins/kisaragirin_onebot/ops.py`：管理指令匹配与执行（`/help`、`/clear`、`/clears`、`/clearl`）。
- `zfnbot/plugins/kisaragirin_onebot/state.py`：群状态、Agent 缓存、清理与关闭逻辑。
- `zfnbot/plugins/kisaragirin_onebot/payload.py`：将 OneBot 平台消息转换为通用 `Message`/`MessageSegment` 对象，构造 `ConversationRequest`。不再处理消息格式渲染。
- `kisaragirin/kisaragirin/message_types.py`：通用消息类型 `Message` / `MessageSegment` 定义与 JSON 序列化辅助函数。
- `zfnbot/plugins/kisaragirin_onebot/config_schema.py`：插件配置结构定义。
- `zfnbot/plugins/kisaragirin_onebot/config.py`：插件实际运行配置。
- `kisaragirin/kisaragirin/agent.py`：Agent 主流程与图装配入口。
- `kisaragirin/kisaragirin/routing.py`：RouteDecision、ExecutionPlan、GraphSpec、ConditionalEdgeSpec 等路由与图规格骨架。
- `kisaragirin/kisaragirin/orchestration.py`：步骤元数据、步骤解析与图装配公共逻辑。
- `kisaragirin/kisaragirin/steps_core.py`：已抽离的核心节点实现（当前包含 `prepare`）。
- `kisaragirin/kisaragirin/steps_response.py`：已抽离的回复与记忆节点实现（当前包含 `reply`、`reply_lite`、`reply_lite_check`、`memory_gate`、`memory`）。
- `kisaragirin/kisaragirin/steps_enrichment.py`：已抽离的增强型节点实现（当前包含 `url`、`vision`、`enrich_merge`、`tools`）。
- `kisaragirin/kisaragirin/steps_routing.py`：路由 step 实现（当前包含 `route`）。
- `kisaragirin/kisaragirin/reply_lite_checks.py`：`reply_lite_check` 节点使用的用语检查函数、评语拼装与规则复用。
- `kisaragirin/kisaragirin/tools.py`：内置工具（`read_url`、可选 `exa_search`、可选 `web_search`〔优先 Exa，回退 Brave〕、可选 `scholar_search`）。
- `kisaragirin/kisaragirin/memory.py`：SQLite 记忆与缓存存储。
- `kisaragirin/kisaragirin/prompts.py`：各步骤提示词文本。
- `kisaragirin/GRAPH_DEVELOPMENT.md`：新增节点与构图开发指南。

## 当前消息处理机制（onebot 插件）

- 仅处理群消息。
- 消息段支持：`text`、`image`、`reply`（`reply` 会递归抓取原消息并嵌入结构，最大深度限制）。
- 图片不直接传 URL 给模型，转为 base64 后放入 `ConversationRequest.images`。
- OneBot 侧仅负责把平台消息（`MessageData`/`MessageSegmentData`）转换成通用 `Message`/`MessageSegment` 对象，通过 `ConversationRequest.messages` 传给 Agent。
- 消息格式渲染（`yaml` / `simple` / `simple-id`）完全由 Agent 内部统一处理。Agent 收到 `list[Message]` 后，根据 `message_format` 渲染成 prompt 文本；历史记忆读出后也重新渲染。
- 队列按 `created_at + sequence` 排序。
- 触发逻辑：
  - 静默 `mention_quiet_seconds` 后，若队列里有 `@bot`，触发一次回复，并引用最后一条 `@` 消息。
  - 若在 `@bot` 的合并窗口内又收到新的 `@bot`，立即将当前窗口中的消息打包为 `bump_snapshot` 强制触发（引用该窗口最后一条 `@`），并将新 `@` 消息保留在队列中开启下一个合并窗口。
  - **竞态条件修复**：`bump` 任务创建时会递增 `pending_bump_count`。当前一个慢回复刚好完成时，`scheduler` 和 `bump task` 会同时被唤醒抢锁。若 `scheduler` 先抢到且发现队列中只剩新 `@`，可能直接触发新 `@`，导致旧 `@` 被延后（出现 1→3→2 的顺序错乱）。修复方式是在 `scheduler` 的 mention 检查中加入 `pending_bump_count > 0` 判断：只要有待处理的 `bump`，`scheduler` 就跳过本轮 mention 触发，等 `bump` 完成后再处理队列，确保旧 `@` 优先于新 `@` 回复（严格 FIFO）。
  - 静默 `idle_start_minutes` 后进入每分钟一次概率抽卡，概率递增，期望在 `idle_expect_minutes` 左右触发。
- 回复执行逻辑：
  - 开始回复时先将当前队列快照并出队（后续新消息不影响本轮）。
  - 共享前段中，URL 总结与图片描述会并行执行，再汇总进入路由。
- 路由阶段使用 `step_models.route` 指定的轻量模型输出路径数组；技术提问、技术文章分享、技术讨论、事实求证、需要工具或分析的内容进入 `default`，情绪化吐槽、闲聊、接梗等进入 `lite_chat`。同一轮消息可同时命中两条路径，随后按数组顺序分别装配对应的独立路径图。`lite_chat` 路径跳过工具调用，并优先使用 `step_models.lite_reply`；若未配置则回退到 `step_models.reply`。
- `lite_chat` 路径内部不是单个 `reply_lite` 节点，而是最多三轮 `reply_lite -> reply_lite_check` 串联。检查节点会依次运行用语检查函数；若某轮未通过，会把全部评语追加到上一版回复末尾，要求 `reply_lite` 重新生成；第三次仍未通过则整条路径取消回复。
- `reply` / `reply_lite` 会先产出路径级回复事件；路径若输出 `bot选择沉默`，则该路径不对外发送。
- 评语是 `reply_lite_check` 产出的编译器风格诊断文本：先定位错误位置，再引用 prompt 中的规则原文说明原因。当前检查器包括：
  - 忽略句首常见语气词（如 `哈*`、`呜*`、`啊`、`诶`、`哎`、`好家伙`、`前辈`）及其后的 `，！。？`，然后检查是否以“这”开头。
  - 用黑名单关键词拦截括号里的动作/状态短语；当前关键词包括 `拍`、`递`、`捂`、`擦`、`晃`、`敲`、`挥`、`躲`、`低头`、`抬头`、`歪头`、`困惑`、`无辜`、`心虚`、`委屈`、`肩`、`脸`、`嘴`、`胸口`、`桌`、`手`、`认错`、`叹气`，以及 `拍肩`、`递零食`、`递奶茶`、`递咖啡`、`困惑脸`、`捂脸`、`小声`；只要括号内容命中关键词就判违规。
  - 直接拦截句尾括号表达：只要 `（...）` / `(...)` 落在行尾或文本末尾就判违规。若有误报，评语会提示在括号后补句号或其他标点。
- 全部路径执行完成后，插件按顺序逐条发送非沉默回复；只有发送成功的路径回复才会在共享 `memory` 收尾阶段一起写回记忆。`reply_lite` 的中间草稿与检查评语不会写入短期记忆，短期记忆只记录最终实际发送的回复。
  - 在 `memory` 完成前，当前群仍保持 replying 状态，下一次回复触发会继续等待/跳过。
  - 若整轮都沉默，不会回灌队列。
  - **Graph 执行失败后的回灌策略**：
    - **idle 模式**：消息回灌队列，但 `queue_version += 1` 并重置 `last_message_at` 为当前时间，强制 scheduler 重新开始 idle 抽卡计时（`next_idle_minute_index = 1`）。
    - **`@` 模式**：消息回灌队列，但**清除所有 `mentioned_bot` 标记**，避免再次触发 mention_quiet；同时 bot 会引用原消息发送 `bot响应@失败`，视为已处理此次 @。
  - 若部分路径已发送成功后才失败，为避免重复发送，不会回灌整轮快照。
  - 若回复成功，不再“全量清空队列”；新进队的消息继续等待下一轮触发。
  - 若当前已有回复在执行：`@` 触发会等待，非 `@` 触发会跳过。

## Agent 流程（kisaragirin）

- `prepare`：组合长期记忆、短期记忆、固定记忆与当前输入。`user_message` 由 `_build_initial_state` 根据 `request.messages` 和 `message_format` 渲染得到。
- `url`：提取 URL，抓取文本并总结；URL 总结会缓存。命中 URL 关键词黑名单时会跳过抓取与缓存命中，直接返回 `禁止读取的url`（当前黑名单包含 `qq.com.cn`）。
- `vision`：处理图片并生成描述；图片描述按 sha256 缓存。
- `enrich_merge`：汇总 `url` 与 `vision` 的补充内容，拼回工作上下文。
- `route`：判断进入哪些路径（可为空、可多选）。
- `tools`：按需调用工具补充信息（仅 `default` 路径）。
  - **工具调用模型说明**：`tool` / `tool_lite` 节点接入的模型可能是上游已封装好的 Agent（例如 OpenClaw、Hermes 等），其内部会自行调用工具并直接返回总结性结果。此时 `AIMessage.tool_calls` 可能为空，但模型输出的文本已包含工具调用后的总结。
  - `tools` / `tools_lite` 节点执行完成后，会将模型最终返回的总结文本作为一条**独立的** `assistant` 消息写入短期记忆。该消息与后续 `reply` / `reply_lite` 产出的回复**不是同一条消息**，且**在 reply 之前**写入，仅用于让 bot 在后续轮次中记得自己查过什么。
  - 写入内容前缀为 `[bot 内部备忘：此内容为工具调用结果，仅自己可见，群友不可见]`。
  - 若同一次执行存在多条路径（如 `default` 与 `lite_chat`），各路径的工具调用结果会按执行顺序聚合，后续路径的 `tools` 节点可直接从 state 中读取先前路径的总结。
  - 即使 bot 最终选择沉默（未发送任何回复），该工具调用记录仍会写入短期记忆，确保 bot 不会遗忘已查询到的信息。
- `reply`：生成技术路径回复文本，只处理技术相关输入，输出技术性内容，长度不超过 150 字；输出 `bot选择沉默` 时取消该路径回复。
- `reply_lite`：生成休闲路径回复文本，只处理休闲/情绪化输入；若收到上一轮检查评语，会基于“上一版回复 + 评语”重写；输出 `bot选择沉默` 时取消该路径回复。
- `reply_lite_check`：顺序执行用语检查函数，写出是否通过与评语；若失败则驱动下一轮 `reply_lite` 重写，连续 3 次失败后取消该路径回复，并记录检查日志。
- `memory_gate`：根据回复发送结果决定是否进入记忆写回。
- `memory`：在全部路径结束后，写回长期记忆与短期记忆。
  - 长期记忆仅在 bot 有成功发送的回复时更新；沉默时保持原状。
  - 短期记忆始终写入本轮 user 输入（以 JSON 序列化的 `list[Message]` 形式存入 SQLite，旧数据兼容 YAML payload 格式）。
  - 无论是否沉默，只要 `tools` / `tools_lite` 节点产出了工具调用总结，都会作为 intermediate assistant 消息在 user 与最终 reply 之间写入短期记忆；多条路径的工具结果按执行顺序全部保留。
  - 当所有路径（含 fallback）均选择沉默时，会额外写入一条 assistant 消息，内容为 `[此消息记录本轮沉默，仅bot自身可见，其他群友未收到任何回复]`，让 bot 记得本轮未输出任何内容。

## 数据与缓存

- 默认 SQLite 文件由 `memory_db_path` 指定（插件配置中设置）。
- 主要表：
  - `long_term_memory`
  - `short_term_memory`
  - `image_description_cache`
  - `url_summary_cache`
- `/clear` 会清除指定 `conversation_id` 的短期/长期记忆，并清空该群当前消息队列。
- `/clears` 只清除短期记忆；`/clearl` 只清除长期记忆；`/help` 返回指令说明。
- 管理指令仅 `ops` 白名单用户可执行，非白名单会返回 `Access Denied`。
- 图片与 URL 缓存表不按会话清空（缓存是全局复用的）。

## 配置来源

- 运行期主要配置在 `zfnbot/plugins/kisaragirin_onebot/config.py`。
- `groups` 即群启用列表与每群 persona/fixed_memory 配置来源。
- `ops` 为管理指令执行权限白名单（QQ 号）。
- `exa_api_key` 用于启用 Exa 的 `web_search`；若为空可回退 `brave_search_api_key`。
- 不再依赖 `.env` 作为插件主配置来源。
- **模型 fallback**：每个 step（节点）在 `AgentConfig.step_fallbacks`（`StepFallbackPools`）中配置独立的 fallback 池子。当主模型调用失败（超时/限流/异常）时，会从该 step 的 fallback 池子里**随机**捞一个备用模型重试，最多重试 `AgentConfig.max_retries` 次（全局配置）。所有 LLM 调用点（`summarize`、`vision`、`reply`、`reply_lite`、`memory`、`tool`、`route` 等）均已接入该机制。fallback 池子里可以包含主模型自身。
- **支持的 LLM provider**：`openai`（OpenAI 兼容接口）、`siliconflow`（硅基流动）、`anthropic`（Anthropic Messages API，对应 `langchain-anthropic`）。

## 日志行为

- `bot.py` 自定义了日志过滤：`kisaragirin*` 与 `zfnbot*` 默认 DEBUG，其它模块（含 nonebot）默认 WARNING。
- 打开 `PLUGIN_CONFIG.debug=True` 后，Agent 的 step 调试内容会通过 `kisaragirin.agent` 日志输出。
- `reply_lite_check` 无论 `debug` 是否开启，都会输出 `LITE-CHECK` 信息日志，记录 attempt、检查器名、通过/失败结果；失败时会附带完整评语。
- 每次完整回复结束后，`kisaragirin.agent` 会统一输出一条性能日志，包含实际运行节点的耗时、`reply_total`（回复产出完成耗时）与 `total`（整轮完成总耗时）。

## 新增管理指令（ops）

新增一条 ops 指令时，**不能只改 `ops.py`**，需要同时修改以下三处，否则 NoneBot 不会把消息路由到处理器：

1. **`zfnbot/plugins/kisaragirin_onebot/ops.py`**
   - 在 `COMMAND_PATTERN` 的正则里加入新指令名。
   - 在 `COMMAND_HELP_TEXT` 里补充说明。
   - 在 `handle_ops_command_event` 中新增分支逻辑。

2. **`zfnbot/plugins/kisaragirin_onebot/__init__.py`**
   - 在 `on_regex` 的 pattern 中加入新指令名，否则消息不会进入 `on_ops_cmd` handler。
   - 同步更新 `PluginMetadata.usage`。

3. **`kisaragirin/kisaragirin/agent.py`**（如需要）
   - 若指令需要调用 Agent 能力（如读写记忆、OpenViking 操作等），在 `KisaragiAgent` 中暴露对应方法，然后在 `ops.py` 中通过 `_get_group_agent(group_id)` 调用。

## 运行方式（本地）

- 安装依赖：`uv sync`
- 启动：`python bot.py`
