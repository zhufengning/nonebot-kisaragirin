# 重构 TODO

按“先降风险，再拆复杂度，最后补工程化”的顺序推进，避免一上来大改把现有行为改坏。

## P0：先做低风险止血

1. [x] 提取 crawler 运行配置
   - 把 `headless`、`verbose`、`user_data_dir` 从 `kisaragirin/kisaragirin/agent.py` 中抽到配置层。
   - 去掉 Linux 路径硬编码，确保 Windows / Linux 都能运行。

2. [x] 清理调试遗留与环境耦合
   - 处理 `test.py` 这类本地手工调试脚本，避免被误认为正式测试。
   - 明确哪些配置是运行必需，哪些只是开发调试用途。

3. [x] 分离配置结构定义与实际配置值
   - 把 `config.py` 中的数据结构定义与具体配置实例拆开。
   - 建议保留独立的 schema / types 文件，再用单独的运行配置文件承载实际值。
   - 避免后续修改配置字段时，同时在“类型定义”和“业务配置”之间来回穿插。

4. [x] 修正文档漂移
   - 更新 `README.md` 与 `zfnbot/plugins/kisaragirin_onebot/README.md` 中和当前实现不一致的描述。
   - 特别是“成功回复后是否清空队列”这类行为说明。

## P1：先拆 onebot 插件入口大文件

5. [x] 拆分消息解析逻辑
   - 从 `zfnbot/plugins/kisaragirin_onebot/__init__.py` 中提取消息解析相关代码到独立模块。
   - 建议拆出：reply 加载、segment 解析、消息序列化准备。

6. [x] 拆分队列与调度逻辑
   - 提取群状态、队列管理、静默触发、idle 抽卡触发。
   - 保持 handler 只负责接收事件和调用服务，降低入口复杂度。

7. [x] 拆分管理指令逻辑
   - 将 `/help`、`/clear`、`/clears`、`/clearl` 从主入口中抽离。
   - 让 ops 命令与普通消息处理解耦。

## P2：再拆 Agent 主流程

8. 分离“流程编排”和“步骤实现”
   - 保留一个统一的步骤定义来源。
   - 避免 LangGraph 流程和 `reply-first` 手写流程各维护一套顺序。
   - 当前进展：已抽出 `kisaragirin/kisaragirin/orchestration.py`，把步骤元数据、步骤解析与图装配公共逻辑从 `agent.py` 中分离；`reply-first` 已开始复用统一的步骤解析与执行器，且返回点由步骤元数据 `emits_reply` 显式定义。

9. 提取 step 处理模块
   - 按职责拆出 URL、图片、工具、回复、记忆步骤。
   - 目标是让 `KisaragiAgent` 只负责装配依赖和组织流程。
   - 当前进展：已抽出 `kisaragirin/kisaragirin/steps_core.py`、`kisaragirin/kisaragirin/steps_enrichment.py` 与 `kisaragirin/kisaragirin/steps_response.py`，并让 `prepare`、`url`、`vision`、`enrich_merge`、`tools`、`reply`、`reply_lite`、`memory_gate`、`memory` 在步骤注册表中直接引用类外实现，去掉了 `agent.py` 中对应的中转薄封装。

10. 收敛并发模型
   - 重新审视线程、后台 event loop、conversation lock、插件侧 task/lock 的边界。
   - 优先减少重复并发控制点，避免未来出现偶发时序 bug。

11. [x] 对齐“发送成功”与“短期记忆写入”语义
   - 重新审视 `reply-first` 流程中 `reply` / `reply_lite` 发送回复与 `memory` 写短期记忆的时序关系。
   - 目标是避免“群消息发送失败，但 assistant 回复已经写入短期记忆”的状态漂移。
   - 明确是否要改为：仅在回复成功发送后，才写入 assistant 短期记忆。


12. [x] 引入 RouteDecision 与执行计划
   - 不把“吹牛 agent / 技术 agent”实现成不同记忆、不同配置的独立 Agent。
   - 先定义 `RouteDecision` / `ExecutionPlan`，只表达 route id、启用的 phase、phase variant 与路由原因。
   - 路由层只负责“选执行路线”，不直接承载记忆、模型配置和工具实例。

13. [x] 抽取共享前置 phase 与共享收尾 phase
   - 把 `prepare`、`url`、`vision` 视为公共 phase，而不是每种 route 各复制一遍完整图。
   - 把 `memory` 作为公共收尾 phase，保证所有 route 最终仍汇合到统一记忆写回逻辑。
   - 为后续把 `url` 与 `vision` 做成并行节点预留结构。

14. [x] 为 route-specific 中段引入 step variant 机制
   - 先把差异最大的阶段做成 variant，例如 `tools.default` / `tools.tech`、`reply.default` / `reply.banter` / `reply.tech`。
   - 如果未来公共 phase 也出现差异，再为公共 phase 增加 variant，而不是复制整张图。
   - 避免把 route 判断直接塞进 `tools`、`reply` 相关函数体内部。

15. [x] 让图装配器按 phase / variant 组图
   - 将 LangGraph 图从“固定线性图”改为“共享骨架 + route 分支”的装配方式。
   - 目标结构：`prepare -> 并行(url, vision) -> route -> route-specific middle -> memory`。
   - 让图只描述 phase 编排，具体执行函数从 step registry / variant registry 解析。

16. [x] 前中后段全部图化，移除线性过渡层
   - 不仅 route-specific middle 要支持图结构，shared prelude 与 shared finalize 也统一表达成图或子图规范。
   - 做完后不再保留“线性 phase 列表 -> 图装配”的过渡逻辑，统一由 graph spec / subgraph builder 直接产出 LangGraph 拓扑。
   - 目标是让 `prepare`、`url/vision` 并行、route middle、`memory` 收尾都处在同一种图抽象之下。

17. 支持 route fan-out：一次触发同时走多条路径
   - 允许同一批新消息在 route 阶段拆成多个并行分支，而不是只能选择单一路径。
   - 典型场景：同一轮消息里同时存在“吹牛上下文”和“技术提问”，分别走不同中段图。
   - 明确分支的输入切片方式：按消息片段、按话题簇、按提及对象，还是按 route scorer 结果分桶。

18. 引入 emission / output-event 机制，替代“单次 return”
   - 不让节点直接把值 return 给外层，而是在图状态中写入 `emission` / `output_event`。
   - 外层执行器监听 emission，按完成顺序流式发送多条消息，而不是等所有路径跑完后一次性堆到最后。
   - 先定义 emission 契约：消息内容、来源路径、是否 final、排序键、去重键。

19. 明确多路径多消息下的发送、记忆与失败语义
   - 约束哪些节点可以 emit，一般只允许 reply 类节点 emit，避免任意节点抢着对外发消息。
   - 明确是否允许单路径多次 emit、不同路径是否都可 emit，以及发送顺序按完成时间还是按优先级。
   - 设计 memory 语义：只记录成功发送的 assistant 消息；部分发送成功时如何写短期记忆、如何重试、如何避免重复发送。
## P3：最后补工程化护栏

20. 为关键行为补自动化测试
    - 至少覆盖：队列快照与失败回灌、reply-first 后 `memory` 收尾、记忆持久化、idle/mention 触发。
    - 先补核心回归测试，再做较大重构。

21. 给 prompt 驱动行为加代码边界
    - 把必须稳定的业务规则从超长 prompt 中抽成代码约束或结构化配置。
    - 减少“改需求只能调提示词”的不确定性。

22. 统一运行与开发说明
    - 明确推荐启动方式、测试方式、调试方式。
    - 减少“README 写一套，实际代码跑另一套”的情况。

## 推荐执行顺序

- 第一阶段：1 -> 2 -> 3 -> 4
- 第二阶段：5 -> 6 -> 7
- 第三阶段：8 -> 9 -> 10 -> 11 -> 12 -> 13 -> 14 -> 15 -> 16 -> 17 -> 18 -> 19
- 第四阶段：20 -> 21 -> 22



















