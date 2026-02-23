# kisaragirin_onebot

这个插件把 OneBot V11 群消息转成结构化输入，交给 `kisaragirin` 处理，并基于队列策略决定回复时机。

## 配置位置

- 插件配置：`zfnbot/plugins/kisaragirin_onebot/config.py`
- 主要配置项：
  - `models` / `step_models`：模型与步骤映射
  - `exa_api_key`：Exa API Key（启用 `exa_search`，并优先用于 `web_search`）
  - `brave_search_api_key`：Brave Search API Key（当 `exa_api_key` 为空时，回退用于 `web_search`）
  - `serpapi_api_key`：SerpApi Key（为空时不启用 `scholar_search` 工具）
  - `groups`：群白名单 + 每群 persona
  - `ops`：可执行管理指令的 QQ 号白名单（非 ops 执行会返回 `Access Denied`）
  - `timing.mention_quiet_seconds`：收到消息后静默 N 秒触发 @ 回复检查
  - `timing.idle_start_minutes`：群聊静默 M 分钟后进入“抽卡回复”
  - `timing.idle_expect_minutes`：抽卡概率增长的期望回复时长
  - `debug`：是否把 `kisaragirin` 的 step 调试日志写到日志系统

## 消息处理流程

1. 接收群消息后，提取文本/图片段并保持段顺序。
2. 图片转为 base64，不把临时 URL 传给模型。
3. 消息进入群队列，按 `event.time + sequence` 排序。
4. 静默到 `mention_quiet_seconds` 后：
   - 若队列中有 @bot 的消息，则触发一次回复；
   - 回复会引用最后一条 @ 消息。
5. 群内静默达到 `idle_start_minutes` 后，每分钟按递增概率抽卡决定是否回复。
6. 成功回复后清空该群队列。

## 输入给 Agent 的格式

- 发送给 `kisaragirin` 的 `ConversationRequest.message` 为 YAML。
- 包含：会话信息、每条消息的发送时间/发送者 id/昵称、`segments` 列表、`merged_text`。
- 图片在 YAML 中用 `[image-x]` 占位，并在 `ConversationRequest.images` 里按同序携带真实图片。

## 记忆与缓存

`kisaragirin` 侧做了以下持久化：

- 短期记忆：保存 user/assistant 轮次；user 文本中的图片占位会转成 `[image-sha256:<hash>]`。
- URL 总结缓存：`url -> summary`。
- 图片描述缓存：`sha256 -> description`。
- 构建新一轮上下文时，会把短期记忆中的 URL/图片重新编号，并在 URL 总结区、图片描述区补齐对应内容。

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
