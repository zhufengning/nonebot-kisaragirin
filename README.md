# nonebot_kisaragirin

基于 NoneBot2 + OneBot V11 的群聊机器人项目，核心对话流程由本地包 `kisaragirin` 提供。

## 目录导航

- `bot.py`：启动入口
- `zfnbot/plugins/kisaragirin_onebot/README.md`：OneBot 插件与消息调度说明
- `kisaragirin/README.md`：Agent 包使用说明
- `kisaragirin/GRAPH_DEVELOPMENT.md`：新增节点、条件边、并行图、构图开发指南
- `TODO.md`：当前重构路线与待办事项
- `AGENTS.md`：项目结构、协作规则与开发摘要

## 配置

- 环境变量示例：复制 `.env.example` 为 `.env.prod` 后按需修改。
- 插件配置示例：复制 `zfnbot/plugins/kisaragirin_onebot/config.example.py` 为 `zfnbot/plugins/kisaragirin_onebot/config.py` 后修改。
- 配置结构定义位于 `zfnbot/plugins/kisaragirin_onebot/config_schema.py`。
- 实际运行配置位于 `zfnbot/plugins/kisaragirin_onebot/config.py`。
- `image_max_upload_bytes` 用于限制单张图片传给模型前的最大字节数；超限时会自动压缩，压不进阈值则跳过该图片。
- 支持可选接入 OpenViking（默认配置示例为 `http://localhost:1933`），用于管理增长型外部记忆。

## 启动

- 安装依赖：`uv sync`
- 启动机器人：`python bot.py`

## 文档

- NoneBot 文档：<https://nonebot.dev/>
- 插件说明：`zfnbot/plugins/kisaragirin_onebot/README.md`
- Agent 包说明：`kisaragirin/README.md`
- 图与节点开发指南：`kisaragirin/GRAPH_DEVELOPMENT.md`
