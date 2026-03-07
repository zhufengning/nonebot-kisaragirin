# bot_renew 项目信息

请在做出任何修改后检查是否需要更新README.md(包括根目录和kisaragirin的)和AGENTS.md

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
- `kisaragirin/kisaragirin/agent.py`：Agent 主流程（step0~step5）与图装配入口。
- `kisaragirin/kisaragirin/routing.py`：RouteDecision / ExecutionPlan 等路由与执行计划骨架。
- `kisaragirin/kisaragirin/orchestration.py`：步骤元数据、步骤解析与图装配公共逻辑。
- `kisaragirin/kisaragirin/steps_core.py`：已抽离的核心 step 实现（当前包含 `step0`、`step1`）。
- `kisaragirin/kisaragirin/steps_response.py`：已抽离的回复与记忆 step 实现（当前包含 `step4`、`step5`）。
- `kisaragirin/kisaragirin/steps_enrichment.py`：已抽离的增强型 step 实现（当前包含 `step2`、`step3`）。
- `kisaragirin/kisaragirin/tools.py`：内置工具（`read_url`、可选 `exa_search`、可选 `web_search`〔优先 Exa，回退 Brave〕、可选 `scholar_search`）。
- `kisaragirin/kisaragirin/memory.py`：SQLite 记忆与缓存存储。
- `kisaragirin/kisaragirin/prompts.py`：各步骤提示词文本。

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
  - step4 产出回复文本后会先发送到群里；只有发送成功后，step5 才会实际写回记忆。
  - 在 step5 完成前，当前群仍保持 replying 状态，下一次回复触发会继续等待/跳过。
  - 若回复失败，会把快照消息回灌队列，避免丢消息。
  - 若回复成功，不再“全量清空队列”；新进队的消息继续等待下一轮触发。
  - 若当前已有回复在执行：`@` 触发会等待，非 `@` 触发会跳过。

## Agent 流程（kisaragirin）

- step0：组合长期记忆、短期记忆、固定记忆与当前输入。
- step1：提取 URL，抓取文本并总结；URL 总结会缓存。
- step2：处理图片并生成描述；图片描述按 sha256 缓存。
- step3：按需调用工具补充信息。
- step4：生成最终回复文本。
- step5：写回长期记忆与短期记忆（user+assistant）。

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
- 每次完整回复结束后，`kisaragirin.agent` 会统一输出一条性能日志，包含 `STEP-0(prepare)` 到 `STEP-5(memory)` 各步耗时和总耗时。

## 运行方式（本地）

- 安装依赖：`uv sync`
- 启动：`python bot.py`














