# kisaragirin_onebot

这个插件把 OneBot V11 群消息转成结构化输入，交给 `kisaragirin` 处理，并基于队列策略决定回复时机。

## 配置位置

- 配置结构定义：`zfnbot/plugins/kisaragirin_onebot/config_schema.py`
- 运行配置：`zfnbot/plugins/kisaragirin_onebot/config.py`
- 配置示例：`zfnbot/plugins/kisaragirin_onebot/config.example.py`

## 主要配置项

- `models` / `step_models`：模型与步骤映射（其中 `step_models.lite_reply` 用于轻量回复路径，留空时回退到 `step_models.reply`）
- `exa_api_key`：Exa API Key（启用 `exa_search`，并优先用于 `web_search`）
- `brave_search_api_key`：Brave Search API Key（当 `exa_api_key` 为空时，回退用于 `web_search`）
- `serpapi_api_key`：SerpApi Key（为空时不启用 `scholar_search` 工具）
- `groups`：群白名单 + 每群 persona
- `short_term_turn_window`：短期记忆保留轮数（按 user+assistant 成对窗口）
- `image_max_upload_bytes`：单张图片传给模型前允许的最大字节数；超限时自动压缩到阈值内
- `ops`：可执行管理指令的 QQ 号白名单（非 ops 执行会返回 `Access Denied`）
- `timing.mention_quiet_seconds`：收到消息后静默 N 秒触发 @ 回复检查
- `timing.idle_start_minutes`：群聊静默 M 分钟后进入“抽卡回复”
- `timing.idle_expect_minutes`：抽卡概率增长的期望回复时长
- `crawler.headless` / `crawler.verbose` / `crawler.user_data_dir`：crawler 运行配置
- `debug`：是否把 `kisaragirin` 的 step 调试日志写到日志系统

## 消息处理流程

1. 接收群消息后，提取文本/图片段并保持段顺序。
2. 静态图片会先按 `image_max_upload_bytes` 做大小检查；超限时自动压缩，再转为 base64，不把临时 URL 传给模型。
3. 动图会保留单个图片占位，但在视觉步骤里按时间顺序抽取最多 5 帧，一次性发给视觉模型，并产出一条合并描述。
4. 消息进入群队列，按 `event.time + sequence` 排序。
5. 静默到 `mention_quiet_seconds` 后：
   - 若队列中有 @bot 的消息，则触发一次回复；
   - 回复会引用最后一条 @ 消息。
6. 群内静默达到 `idle_start_minutes` 后，每分钟按递增概率抽卡决定是否回复。
7. 开始回复时会先取当前队列快照并出队；成功回复后不会清空后续新进队消息，失败时会把快照消息回灌队列。
8. 共享前段中，URL 总结与图片描述会并行执行，再汇总进入路由与后续回复路径。
9. 路由阶段会使用 `step_models.route` 指定的轻量模型输出路径数组；技术提问、技术文章分享、技术讨论、事实求证、需要工具或分析的内容进入 `default`，情绪化吐槽、闲聊、接梗等进入 `lite_chat`。同一轮消息可以同时命中两条路径。`default` 继续工具调用后回复，`lite_chat` 直接走轻量聊天路径，并优先使用 `step_models.lite_reply`；若未配置，则回退到 `step_models.reply`。
10. 技术路径与休闲路径都会显式要求“只处理属于自己路径的消息，其余输入由其他路径处理”；若某条路径筛完后没有该它回复的内容，会输出 `bot选择沉默`，并取消该路径的对外发送。技术路径回复会限制为不超过 150 字的技术性内容。
11. `lite_chat` 路径内部会最多执行 3 轮 `reply_lite -> reply_lite_check`。检查失败时，会把所有评语追加到上一版回复末尾后要求重写；第 3 次仍不通过则取消该路径回复。当前检查器会先忽略句首语气词及其后的 `，！。？`，再检查是否以“这”开头；还会用黑名单拦截 `（拍肩）`、`（递零食）`、`（递奶茶）`、`（递咖啡）`、`（困惑脸）`、`（捂脸）`、`（小声）` 及其半角括号版本，并用高置信模板扫描短括号表达，只要命中动作/状态词典中的任一词就判违规。
12. Agent 会把每条非沉默路径产出为独立输出事件；插件按顺序逐条发送。若整轮都沉默，则本轮正常消费队列但不发消息。
13. 共享 `memory` 收尾会等全部路径结束且发送阶段完成后再执行，只把实际成功发送的路径回复一起写回记忆。`reply_lite` 的中间草稿和检查评语不会进入短期记忆。若部分路径已成功发送、后续发送失败，为避免重复发送，不会回灌整轮快照。

## 输入给 Agent 的格式

- 发送给 `kisaragirin` 的 `ConversationRequest.message` 为 YAML。
- 包含：会话信息、每条消息的发送时间/发送者 id/昵称、`segments` 列表、`merged_text`。
- 图片在 YAML 中用 `[image-x]` 占位，并在 `ConversationRequest.images` 里按同序携带真实图片。

## 记忆与缓存

`kisaragirin` 侧做了以下持久化：

- 短期记忆：保存 user/assistant 轮次；user 文本中的图片占位保持为 `[image-数字]`。
- URL 总结缓存：`url -> summary`。
- 图片描述缓存：`sha256 -> description`。
- 构建新一轮上下文时，会把短期记忆中的 URL/图片重新编号，并在 URL 总结区、图片描述区补齐对应内容（展示仅保留图片编号，不显示 sha256）。

## 指令

- `/help`：查看指令帮助。
- `/clear`：清空当前群的消息队列，并删除该群 conversation 的短期/长期记忆。
- `/clears`：只清除该群 conversation 的短期记忆。
- `/clearl`：只清除该群 conversation 的长期记忆。

## 典型日志

- `bot mentioned ...`
- `mention quiet timeout reached ...`
- `idle draw ... probability=... draw=... hit=...`
- `reply trigger=...`
- `reply sent trigger=...`
