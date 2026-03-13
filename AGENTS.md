# bot_renew 项目信息

请在做出任何修改后检查是否需要更新README, AGENTS.md以及其他文档。

请在修改代码后使用ty check和ruff check检查并修复报错。

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
- `zfnbot/plugins/kisaragirin_onebot/payload.py`：将消息序列化为 YAML，并构造 `ConversationRequest`。
- `zfnbot/plugins/kisaragirin_onebot/config_schema.py`：插件配置结构定义。
- `zfnbot/plugins/kisaragirin_onebot/config.py`：插件实际运行配置。
- `kisaragirin/kisaragirin/agent.py`：Agent 主流程与图装配入口。
- `kisaragirin/kisaragirin/routing.py`：RouteDecision、ExecutionPlan、GraphSpec、ConditionalEdgeSpec 等路由与图规格骨架。
- `kisaragirin/kisaragirin/orchestration.py`：步骤元数据、步骤解析与图装配公共逻辑。
- `kisaragirin/kisaragirin/steps_core.py`：已抽离的核心节点实现（当前包含 `prepare`）。
- `kisaragirin/kisaragirin/steps_response.py`：已抽离的回复与记忆节点实现（当前包含 `reply`、`reply_lite`、`memory_gate`、`memory`）。
- `kisaragirin/kisaragirin/steps_enrichment.py`：已抽离的增强型节点实现（当前包含 `url`、`vision`、`enrich_merge`、`tools`）。
- `kisaragirin/kisaragirin/steps_routing.py`：路由 step 实现（当前包含 `route`）。
- `kisaragirin/kisaragirin/tools.py`：内置工具（`read_url`、可选 `exa_search`、可选 `web_search`〔优先 Exa，回退 Brave〕、可选 `scholar_search`）。
- `kisaragirin/kisaragirin/memory.py`：SQLite 记忆与缓存存储。
- `kisaragirin/kisaragirin/prompts.py`：各步骤提示词文本。
- `kisaragirin/GRAPH_DEVELOPMENT.md`：新增节点与构图开发指南。

## 当前消息处理机制（onebot 插件）

- 仅处理群消息。
- 消息段支持：`text`、`image`、`reply`（`reply` 会递归抓取原消息并嵌入结构，最大深度限制）。
- 图片不直接传 URL 给模型，转为 base64 后放入 `ConversationRequest.images`。
- 发给 Agent 的正文是 YAML，保留 message 与 segment 层级关系。
- 队列按 `created_at + sequence` 排序。
- 触发逻辑：
  - 静默 `mention_quiet_seconds` 后，若队列里有 `@bot`，触发一次回复，并引用最后一条 `@` 消息。
  - 静默 `idle_start_minutes` 后进入每分钟一次概率抽卡，概率递增，期望在 `idle_expect_minutes` 左右触发。
- 回复执行逻辑：
  - 开始回复时先将当前队列快照并出队（后续新消息不影响本轮）。
  - 共享前段中，URL 总结与图片描述会并行执行，再汇总进入路由。
- 路由阶段使用 `step_models.route` 指定的轻量模型输出路径数组；技术提问、技术文章分享、技术讨论、事实求证、需要工具或分析的内容进入 `default`，情绪化吐槽、闲聊、接梗等进入 `lite_chat`。同一轮消息可同时命中两条路径，随后按数组顺序分别装配对应的独立路径图。`lite_chat` 路径跳过工具调用，并优先使用 `step_models.lite_reply`；若未配置则回退到 `step_models.reply`。
- `reply` / `reply_lite` 会先产出路径级回复事件；路径若输出 `bot选择沉默`，则该路径不对外发送。
- 全部路径执行完成后，插件按顺序逐条发送非沉默回复；只有发送成功的路径回复才会在共享 `memory` 收尾阶段一起写回记忆。
  - 在 `memory` 完成前，当前群仍保持 replying 状态，下一次回复触发会继续等待/跳过。
  - 若整轮都沉默，不会回灌队列。
  - 若尚未发送任何回复就失败，会把快照消息回灌队列，避免丢消息。
  - 若部分路径已发送成功后才失败，为避免重复发送，不会回灌整轮快照。
  - 若回复成功，不再“全量清空队列”；新进队的消息继续等待下一轮触发。
  - 若当前已有回复在执行：`@` 触发会等待，非 `@` 触发会跳过。

## Agent 流程（kisaragirin）

- `prepare`：组合长期记忆、短期记忆、固定记忆与当前输入。
- `url`：提取 URL，抓取文本并总结；URL 总结会缓存。
- `vision`：处理图片并生成描述；图片描述按 sha256 缓存。
- `enrich_merge`：汇总 `url` 与 `vision` 的补充内容，拼回工作上下文。
- `route`：判断进入哪些路径（可为空、可多选）。
- `tools`：按需调用工具补充信息（仅 `default` 路径）。
- `reply`：生成技术路径回复文本，只处理技术相关输入，输出技术性内容，长度不超过 150 字；输出 `bot选择沉默` 时取消该路径回复。
- `reply_lite`：生成休闲路径回复文本，只处理休闲/情绪化输入；输出 `bot选择沉默` 时取消该路径回复。
- `memory_gate`：根据回复发送结果决定是否进入记忆写回。
- `memory`：在全部路径结束后，写回长期记忆与短期记忆（user+assistant），并合并本轮所有成功发送的路径回复。

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

## 日志行为

- `bot.py` 自定义了日志过滤：`kisaragirin*` 与 `zfnbot*` 默认 DEBUG，其它模块（含 nonebot）默认 WARNING。
- 打开 `PLUGIN_CONFIG.debug=True` 后，Agent 的 step 调试内容会通过 `kisaragirin.agent` 日志输出。
- 每次完整回复结束后，`kisaragirin.agent` 会统一输出一条性能日志，包含实际运行节点的耗时、`reply_total`（回复产出完成耗时）与 `total`（整轮完成总耗时）。

## 运行方式（本地）

- 安装依赖：`uv sync`
- 启动：`python bot.py`
