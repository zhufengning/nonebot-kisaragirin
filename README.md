# nonebot_kisaragirin

基于 NoneBot2 + OneBot V11 的群聊机器人项目，核心对话流程由本地包 `kisaragirin` 提供。

## 配置

- 环境变量示例：复制 `.env.example` 为 `.env.prod` 后按需修改。
- 插件配置示例：复制 `zfnbot/plugins/kisaragirin_onebot/config.example.py` 为 `zfnbot/plugins/kisaragirin_onebot/config.py` 后修改。
- 配置结构定义位于 `zfnbot/plugins/kisaragirin_onebot/config_schema.py`。
- 实际运行配置位于 `zfnbot/plugins/kisaragirin_onebot/config.py`。

## 启动

- 安装依赖：`uv sync`
- 启动机器人：`python bot.py`

## 文档

- NoneBot 文档：<https://nonebot.dev/>
- 插件说明：`zfnbot/plugins/kisaragirin_onebot/README.md`
- Agent 包说明：`kisaragirin/README.md`
