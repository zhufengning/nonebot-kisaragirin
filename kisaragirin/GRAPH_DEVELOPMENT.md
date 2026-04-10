# Graph 开发指南

这份文档面向维护 `kisaragirin` 图流程的开发者，说明如何：

- 添加一个新的 step 节点
- 把新节点接入当前图系统
- 构建顺序、并行、条件分支与收尾节点

本文以当前仓库中的实现为准。

## 1. 当前架构总览

当前图系统分四层：

1. `steps_*.py`
   - 放具体 step 实现
   - 例如：`steps_core.py`、`steps_enrichment.py`、`steps_response.py`、`steps_routing.py`

2. `agent.py`
   - 提供运行时依赖
   - 通过 `_step_implementations()` 把 phase/variant 映射到具体函数
   - 提供模型、记忆、日志、工具等 runtime 能力

3. `routing.py`
   - 定义图规格（`GraphSpec`）
   - 定义默认路由、默认图骨架、条件边
   - 负责把 shared prelude / route middle / shared finalize 组合成 `ExecutionPlan`

4. `orchestration.py`
   - 负责把 `ExecutionPlan` 解析成可执行节点
   - 负责自定义执行器中的条件边、并行批执行、reply-first 截断语义
   - 负责把图规格编译成 LangGraph 原生图

## 2. 一个节点由哪几部分组成

增加一个新节点，通常需要动以下几个位置：

### 2.1 写节点实现

把节点实现放到合适的 `steps_*.py` 文件里。

示例：

```python
def run_memory_gate(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    should_update_memory = bool(state.get("assistant_reply_sent", True))
    result = "update" if should_update_memory else "skip"
    return {
        "memory_gate_result": result,
        "step_attachments": agent._set_attachment(
            state,
            "memory_gate",
            f"memory_gate_result={result}",
        ),
    }
```

建议遵循：

- 输入签名固定为：`(agent, state) -> dict`
- 返回值只返回**本节点新增/更新的 state 字段**
- 不直接操纵图结构，不直接决定跳转目标
- 节点内部如需日志，调用 `agent._log_step_debug(...)`
- 节点内部如需附件，调用 `agent._set_attachment(...)`

### 2.2 注册节点实现

在 `kisaragirin/kisaragirin/agent.py` 的 `_step_implementations()` 中注册：

```python
"memory_gate": {
    "default": lambda state: run_memory_gate(self, state),
},
```

这里的 key 分别是：

- 第一层：`phase`
- 第二层：`variant`

也就是说，一个 phase 可以有多个变体，例如：

```python
"reply": {
    "default": lambda state: run_reply(self, state),
    "lite": lambda state: run_reply_lite(self, state),
},
```

### 2.3 注册节点元数据

在 `kisaragirin/kisaragirin/orchestration.py` 的 `DEFAULT_STEP_METADATA` 中注册：

```python
"memory_gate": {
    "default": StepMetadata("memory_gate", "memory_gate"),
},
```

这里定义的是：

- `step_name`：调试日志与 step 附件使用的内部名称
- `default_node_name`：默认节点名
- `step_class`：节点类别；普通节点默认是 `default`
- `output_key`：reply 类节点向状态里写出的输出字段

如果一个节点属于 reply 类，推荐直接使用：

```python
"reply": {
    "default": reply_step_metadata("reply", "reply"),
},
```

这会把节点注册成 `step_class="reply"`，并声明它使用 `reply` 作为输出字段。后续如果要增加格式审核、改写、合规检查等“发言链路里的节点”，可以继续复用这个入口，只在真正对外产出消息的那个节点上注册为 reply 类。

## 3. 如何把节点接入图

当前图不是直接手写 LangGraph 节点连接，而是先在 `routing.py` 中写 `GraphSpec`。

### 3.1 图规格基础类型

`routing.py` 中有这些核心类型：

- `GraphNodeSpec`
  - 表示一个节点
  - 包含：`node_id`、`phase`、`variant`

- `ConditionalEdgeSpec`
  - 表示条件边
  - 包含：
    - `source_node_id`
    - `condition_key`
    - `branches`
    - `default_target_node_id`

- `GraphSpec`
  - 表示一张图
  - 包含：
    - `nodes`
    - `edges`
    - `entry_node_ids`
    - `exit_node_ids`
    - `conditional_edges`

### 3.2 顺序连接

顺序图最简单：

```python
GraphSpec(
    nodes=(
        GraphNodeSpec(node_id="tools", phase="tools"),
        GraphNodeSpec(node_id="reply", phase="reply"),
    ),
    edges=(("tools", "reply"),),
    entry_node_ids=("tools",),
    exit_node_ids=("reply",),
)
```

### 3.3 并行连接

如果两个节点共享同一个前驱，并且后面汇合到同一个节点，就能表达并行。

当前仓库里 `url` / `vision` 的并行前段就是：

```python
GraphSpec(
    nodes=(
        GraphNodeSpec(node_id="prepare", phase="prepare"),
        GraphNodeSpec(node_id="url", phase="url"),
        GraphNodeSpec(node_id="vision", phase="vision"),
        GraphNodeSpec(node_id="enrich_merge", phase="enrich_merge"),
    ),
    edges=(
        ("prepare", "url"),
        ("prepare", "vision"),
        ("url", "enrich_merge"),
        ("vision", "enrich_merge"),
    ),
    entry_node_ids=("prepare",),
    exit_node_ids=("enrich_merge",),
)
```

注意：

- 并行节点不要同时写冲突的 state key
- 更稳的做法是：
  - `url` 写 `url_appendix`
  - `vision` 写 `vision_appendix`
  - 由 `enrich_merge` 统一合并到 `working_text`

### 3.4 条件分支

当前项目仍支持声明式条件边，但主要用于图内 gate，例如 `memory_gate -> memory / END`。

对于 `route`，当前实现不再把 `default` 和 `lite_chat` 两条路径写在同一张图里，而是拆成三段：

- `shared_prelude_graph`：共享前段（prepare/url/vision/enrich_merge）
- `route_selector_graph`：只负责执行 `route` 节点并写出 `route_choices`
- `route_graph`：根据 `route_choices` 中的每个 route id 依次装配独立路径图，例如 `default_route_graph` 或 `lite_chat_route_graph`

也就是说：

- `route` 节点只负责在 state 里写 `route_choices`
- Agent 运行时会先跑共享前段和选路图，再按 `route_choices` 逐条装配对应的独立路径图继续执行
- 当某条路径已经产出非沉默回复时，后续路径会额外在输入里看到 `[THIS-TURN-ALREADY-SENT]`，把这些 assistant 消息当作本轮消息记录末尾已经出现的内容

这种方式的优点是：

- 每条路由路径都是独立图，结构更清晰
- 路径内节点不会混在同一张图里
- 改某条路径结构时，不必维护另一条路径的条件边

### 3.5 路由节点

当前仓库里的 `route` 节点会使用 `step_models.route` 指定的轻量模型，根据输入里给出的路径描述输出一个 route id 数组；判断完成后，Agent 会按数组顺序装配对应的独立路径图继续执行。数组允许为空，表示整轮选择沉默。

注意：`lite_chat` 路径虽然跳过工具调用，但回复节点会优先使用 `step_models.lite_reply`；若未配置，则回退到 `step_models.reply`。
另外，当前 `lite_chat` 路径不是单个 `reply_lite` 节点，而是展开成最多三轮 `reply_lite -> reply_lite_check` 的无环图。检查失败时，会把评语附在上一版回复末尾交给下一轮 `reply_lite` 重写；第三轮仍失败则直接结束，不产生输出事件。当前已接入的检查函数既包括“去掉句首语气词后禁止以‘这’开头”，也包括括号黑名单与“句尾括号一刀切”规则。

### 3.6 收尾闸门

如果某些节点只应在满足条件时执行，推荐显式加 gate 节点。

当前仓库里的 `memory_gate` 就是这种模式：

- `memory_gate` 节点先算出 `memory_gate_result`
- 条件边决定：
  - `update -> memory`
  - `skip -> END`

这比把判断直接塞进 `memory` 节点本身更清楚。

## 4. reply-first 是怎么接的

当前 reply-first 不是单独维护一套步骤顺序，而是复用 `ExecutionPlan` 和 `GraphSpec`：

- 先按图规格解析出节点
- 找到 reply 类节点（例如 `reply` 或 `reply_lite`），把其输出收集成 `OutputEvent`
- 全部路径中段结束后，再统一进入 finalize 部分（例如 `memory_gate`、`memory`）

因此如果你加了新的 reply 节点，需要确认：

1. 它是否应成为对外返回点
2. 若是，就用 `reply_step_metadata(...)` 把它注册成 reply 类节点

如果一条执行路径里没有任何 reply 类节点，它就不会产生输出事件。

注意：`reply_lite_check` 这类“审核/改写驱动节点”不应注册成 reply 类节点。真正对外产出的仍然只有 `reply_lite_*` 节点，检查节点只负责写条件键、评语和日志。

## 5. 如何新增一个新节点：推荐步骤

以新增 `planner` 节点为例：

### 步骤 1：写实现

新建或放入合适文件，例如 `steps_planner.py`：

```python
def run_planner(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    ...
```

### 步骤 2：注册到 `_step_implementations()`

```python
"planner": {
    "default": lambda state: run_planner(self, state),
},
```

### 步骤 3：注册 `DEFAULT_STEP_METADATA`

```python
"planner": {
    "default": StepMetadata("planner", "planner"),
},
```

### 步骤 4：把节点放进图规格

如果是默认中段的一部分：

```python
GraphNodeSpec(node_id="planner", phase="planner")
```

再配合边：

```python
("tools", "planner"),
("planner", "reply"),
```

### 步骤 5：检查状态字段是否冲突

如果它和并行节点并发执行，确保：

- 不要直接覆盖共享 key
- 用中间字段 + merge 节点来聚合

### 步骤 6：更新文档

如果这个节点是公共能力，记得同步：

- `AGENTS.md`
- 相关 `README.md`

## 6. 当前推荐实践

### 6.1 节点职责尽量单一

- 节点实现只做业务计算
- 图结构放到 `routing.py`
- 步骤解析与图装配放到 `orchestration.py`

### 6.2 并行节点避免冲突写入

推荐：

- 并行节点写独立字段
- 合并节点统一整理成主上下文字段

### 6.3 条件分支用声明式映射

推荐：

- 节点只写 `condition_key`
- `ConditionalEdgeSpec` 决定下一步去哪

不推荐：

- 节点内部直接返回“下一个节点 id”

### 6.4 gate 节点优于隐式判断

推荐把：

- 是否更新记忆
- 是否允许发消息
- 是否进入工具路径

做成显式 gate 节点，而不是直接埋在下游节点内部。

## 7. 目前的局限

当前系统已经支持：

- 顺序图
- 并行批执行
- 条件分支
- route fan-out 多路径执行
- `OutputEvent` 输出契约
- reply-first 统一收尾记忆

但还没有正式支持：

- 通用循环控制（虽然底层可由条件边 + 回边表达）

后续如果要加这些能力，建议继续在 `GraphSpec` 和 `orchestration.py` 层扩展，而不要把逻辑重新塞回 `agent.py`。
