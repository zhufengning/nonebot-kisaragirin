# kisaragirin_onebot

这个插件把 OneBot V11 群消息转成结构化输入，交给 `kisaragirin` 处理，并基于队列策略决定回复时机。

## 配置位置

- 配置结构定义：`zfnbot/plugins/kisaragirin_onebot/config_schema.py`
- 运行配置：`zfnbot/plugins/kisaragirin_onebot/config.py`
- 配置示例：`zfnbot/plugins/kisaragirin_onebot/config.example.py`

## NapCat 说明

- 如果使用的是 NapCat，想让合并转发消息在接收侧拿到 `forward.content` 并展开解析，需要在 NapCat 设置里打开“启用上报解析合并消息”。
- 若未开启，该插件通常只能收到 `forward.id`，无法直接拿到转发内的具体消息内容。

## 主要配置项

- `models` / `step_models`：模型与步骤映射（其中 `step_models.lite_reply` 用于轻量回复路径，留空时回退到 `step_models.reply`）
- `exa_api_key`：Exa API Key（启用 `exa_search`，并优先用于 `web_search`）
- `brave_search_api_key`：Brave Search API Key（当 `exa_api_key` 为空时，回退用于 `web_search`）
- `serpapi_api_key`：SerpApi Key（为空时不启用 `scholar_search` 工具）
- `openviking`：可选 OpenViking 配置；启用后会在 `url` / `vision` 完成后检索 OpenViking 记忆，并在 `memory` 收尾时提交本轮对话与工具结果到 OpenViking
  - OneBot 插件会显式使用这里配置的 HTTP 参数初始化 Python client，不依赖 `ov` CLI 的 `ovcli.conf`
  - 若直接配置共享 `api_key`，所有群会落到同一个 OpenViking user memory 命名空间，存在跨群召回风险
  - 推荐改成 `root_api_key + account + conversation_user_prefix`：插件会为每个群自动创建独立 OpenViking user，把返回的 `user_key` 缓存在本地 SQLite，再按群隔离检索/提交记忆
  - 检索时不再额外限制 `target_uri`；是否隔离完全取决于当前群对应的 OpenViking user key
- `groups`：群白名单 + 每群 persona
- `message_format`：发送给 LLM 的消息格式；`yaml` 保留结构化层级，`simple` 渲染成接近 QQ 聊天记录的纯文本块
- `short_term_turn_window`：短期记忆保留轮数（按 user+assistant 成对窗口）
- `image_max_upload_bytes`：单张图片传给模型前允许的最大字节数；超限时自动压缩到阈值内
- `ops`：可执行管理指令的 QQ 号白名单（非 ops 执行会返回 `Access Denied`）
- `timing.mention_quiet_seconds`：收到消息后静默 N 秒触发 @ 回复检查
- `timing.idle_start_minutes`：群聊静默 M 分钟后进入“抽卡回复”
- `timing.idle_expect_minutes`：抽卡概率增长的期望回复时长
- `crawler.headless` / `crawler.verbose` / `crawler.user_data_dir`：crawler 运行配置
- `debug`：是否把 `kisaragirin` 的 step 调试日志写到日志系统

## 消息处理流程

1. 接收群消息后，提取并规范化常见 OneBot 段，保持段顺序。当前会显式接收 `text`、`at/mention`、`reply`、`face`、`image`、`record`、`video`、`file`、`json`、`forward`、`poke`、`dice`、`rps`；NapCat 的 `mface` 若已转成 `image`，按图片处理。
2. 静态图片会先按 `image_max_upload_bytes` 做大小检查；超限时自动压缩，再转为 base64，不把临时 URL 传给模型。
3. 动图会保留单个图片占位，但在视觉步骤里按时间顺序抽取最多 5 帧，一次性发给视觉模型，并产出一条合并描述。
4. 消息进入群队列，按 `event.time + sequence` 排序。
5. 静默到 `mention_quiet_seconds` 后：
   - 若队列中有 @bot 的消息，则触发一次回复；
   - 回复会引用最后一条 @ 消息。
6. 群内静默达到 `idle_start_minutes` 后，每分钟按递增概率抽卡决定是否回复。
7. 开始回复时会先取当前队列快照并出队；成功回复后不会清空后续新进队消息，失败时会把快照消息回灌队列。
8. 共享前段中，URL 总结与图片描述会并行执行；随后再检索一次 OpenViking 记忆，最后汇总进入路由与后续回复路径。
9. 路由阶段会使用 `step_models.route` 指定的轻量模型输出路径数组；技术提问、技术文章分享、技术讨论、事实求证、需要工具或分析的内容进入 `default`，情绪化吐槽、闲聊、接梗等进入 `lite_chat`。同一轮消息可以同时命中两条路径。`default` 继续工具调用后回复，`lite_chat` 直接走轻量聊天路径，并优先使用 `step_models.lite_reply`；若未配置，则回退到 `step_models.reply`。
10. 技术路径与休闲路径都会显式要求“只处理属于自己路径的消息，其余输入由其他路径处理”；若某条路径筛完后没有该它回复的内容，会输出 `bot选择沉默`，并取消该路径的对外发送。技术路径回复会限制为不超过 150 字的技术性内容。若本轮前面的路径已经产出回复，后面的路径会在输入中额外看到 `[THIS-TURN-ALREADY-SENT]`，把这些内容视为自己刚刚已经发出的消息，避免重复，必要时主动沉默。
11. `lite_chat` 路径内部会最多执行 3 轮 `reply_lite -> reply_lite_check`。检查失败时，会把所有评语追加到上一版回复末尾后要求重写；第 3 次仍不通过则取消该路径回复。当前检查器会先忽略句首语气词及其后的 `，！。？`，再检查是否以“这”开头；还会用黑名单关键词拦截括号内容，当前关键词包括 `拍`、`递`、`捂`、`擦`、`晃`、`敲`、`挥`、`躲`、`低头`、`抬头`、`歪头`、`困惑`、`无辜`、`心虚`、`委屈`、`肩`、`脸`、`嘴`、`胸口`、`桌`、`手`、`认错`、`叹气`，以及 `拍肩`、`递零食`、`递奶茶`、`递咖啡`、`困惑脸`、`捂脸`、`小声`，并直接拦截句尾括号表达。若有误报，可在括号后补句号或其他标点。
12. Agent 会把每条非沉默路径产出为独立输出事件；插件按顺序逐条发送。若整轮都沉默，则本轮正常消费队列但不发消息。
13. 共享 `memory` 收尾会等全部路径结束且发送阶段完成后再执行，只把实际成功发送的路径回复一起写回记忆。`reply_lite` 的中间草稿和检查评语不会进入短期记忆。若部分路径已成功发送、后续发送失败，为避免重复发送，不会回灌整轮快照。
14. 若启用了 OpenViking，共享前段会在 `url` / `vision` 完成后执行一次 `search()` 召回 OpenViking 记忆；检索输入会附带 URL/图片摘要，并明确说明 `[url-x]`、`[image-x]` 只是当前输入里的临时标号。`memory` 会在 SQLite 写回后，把本轮 user、这些补充摘要、最终发送成功的 assistant 回复，以及 `default` 路径中实际发生的工具调用结果写入 OpenViking session 并执行 `commit()`。OpenViking 失败不会影响主回复。
15. 为避免跨群召回，HTTP 模式推荐使用 `root_api_key + account + conversation_user_prefix`。插件会为每个 `group_id` 自动创建独立 OpenViking user，并把 `user_key` 缓存在同一个 SQLite 数据库里的 `openviking_user_keys` 表；如果服务端该 user 已存在但本地没有缓存，会自动调用 Admin API 轮换 key 后再落库。
16. 插件启动时会预热所有启用群的 agent；若 OpenViking 配置无效或初始化失败，启动阶段会直接抛错退出，而不是等到首条消息时才暴露问题。

## 输入给 Agent 的格式

- `message_format=yaml` 时，发送给 `kisaragirin` 的 `ConversationRequest.message` 为 YAML。
- `message_format=simple` 时，会在发送前把同一份 YAML 渲染成接近 QQ 聊天记录的纯文本：
  - 每条消息渲染为 `[昵称]: 内容`，并用 `---` 分隔。
  - 若消息 `@bot`，会在最前面插入 `(有人@我)`。
  - reply 只展开 1 层，渲染成下一行缩进的 `  [ref 昵称]：内容`。
  - forward 若携带解析后的内容，也只展开 1 层；每条转发子消息各占一行，格式为 `  [forward 昵称]：内容`。
  - 连续消息会按 3 分钟窗口插入一个精确到分钟的时间块，时间块同样用 `---` 包裹。
  - 其余非文本段会用占位文本保留：`face` 渲染为 `[face: 名称]`，`record` 渲染为 `[record: 语音]`，`video/file` 渲染为 `[{type}: 文件名]`，`json` 渲染为 `[json: 原文]`，`poke/dice/rps` 渲染为 `[{type}: 关键信息]`。
- 无论选择哪种 `message_format`，短期记忆里持久化的 user 输入都仍然保存为 YAML。
- 但在 `message_format=simple` 下，`[SHORT-TERM-CONTEXT]` 会在读取这些 YAML 记忆时临时重新渲染成简化聊天记录，保证喂给 LLM 的上下文风格一致。
- YAML 会在规范化字段之外尽量额外保留原始 OneBot `data` 字段；图片在 YAML/简化文本中都保持 `[image-x]` 占位，并在 `ConversationRequest.images` 里按同序携带真实图片。

## 记忆与缓存

`kisaragirin` 侧做了以下持久化：

- 短期记忆：保存 user/assistant 轮次；user 文本中的图片占位保持为 `[image-数字]`。assistant 也会以结构化消息写回，并显式标记为 bot 自己发送，旧的纯文本 assistant 记忆在读取时会自动兼容。
- OpenViking（可选）：作为外部增长型记忆仓库。当前实现会在 `url` / `vision` 后做一次 `search()` 召回，在 `memory` 收尾时执行 `commit()` 写入；两边都会附带 URL/图片摘要，并显式声明 `[url-x]`、`[image-x]` 只是当前输入内的临时标号。不会覆盖本地 `fixed_memory` 或 SQLite 长期记忆。写入时基础 user 文本跟随 `message_format`：`yaml` 模式写 YAML，`simple` 模式写简化聊天文本。
- 若配置 `root_api_key + account + conversation_user_prefix`，插件会把每个群映射到独立 OpenViking user；若只配置共享 `api_key`，则所有群共享同一 OpenViking user memory 命名空间。
- URL 总结缓存：`url -> summary`。
- 图片描述缓存：`sha256 -> description`。
- URL 若命中关键词黑名单，会跳过抓取与缓存读取，直接返回 `禁止读取的url`；当前黑名单包含 `qq.com.cn`。
- 构建新一轮上下文时，会把短期记忆中的 URL/图片重新编号，并在 URL 总结区、图片描述区补齐对应内容（展示仅保留图片编号，不显示 sha256）。

## 指令

- `/help`：查看指令帮助。
- `/clear`：清空当前群的消息队列，并删除该群 conversation 的短期/长期记忆。
- `/clears`：只清除该群 conversation 的短期记忆。
- `/clearl`：只清除该群 conversation 的长期记忆。
- `/ov_init_commit`：将当前群现有的本地长期记忆手动 bootstrap 提交一次到 OpenViking。
- `/clear_empty_cache`：清理 `url_summary_cache` / `image_description_cache` 中去掉空白后为空的缓存条目。

## 典型日志

- `bot mentioned ...`
- `mention quiet timeout reached ...`
- `idle draw ... probability=... draw=... hit=...`
- `reply trigger=...`
- `reply sent trigger=...`
