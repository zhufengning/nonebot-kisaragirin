from __future__ import annotations

from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from .routing import ConditionalEdgeSpec, END_TARGET, ExecutionPlan, GraphSpec

StepHandler = Callable[[Any], Any]
StepImplementationRegistry = dict[str, dict[str, StepHandler]]


@dataclass(slots=True, frozen=True)
class StepMetadata:
    step_name: str
    default_node_name: str
    emits_reply: bool = False


DEFAULT_STEP_METADATA: dict[str, dict[str, StepMetadata]] = {
    "prepare": {
        "default": StepMetadata("prepare", "prepare"),
    },
    "url": {
        "default": StepMetadata("url", "url"),
    },
    "vision": {
        "default": StepMetadata("vision", "vision"),
    },
    "enrich_merge": {
        "default": StepMetadata("enrich_merge", "enrich_merge"),
    },
    "route": {
        "default": StepMetadata("route", "route"),
    },
    "tools": {
        "default": StepMetadata("tools", "tools"),
    },
    "reply": {
        "default": StepMetadata("reply", "reply", emits_reply=True),
        "lite": StepMetadata("reply_lite", "reply_lite", emits_reply=True),
    },
    "memory_gate": {
        "default": StepMetadata("memory_gate", "memory_gate"),
    },
    "memory": {
        "default": StepMetadata("memory", "memory"),
    },
}


@dataclass(slots=True, frozen=True)
class ResolvedStep:
    node_id: str
    phase: str
    variant: str
    step_name: str
    node_name: str
    handler: StepHandler
    emits_reply: bool = False


@dataclass(slots=True)
class GraphExecutionCursor:
    completed_node_ids: set[str]
    selected_branch_targets: dict[str, str]


def resolve_phase_variant(execution_plan: ExecutionPlan, phase: str) -> str:
    return str(execution_plan.phase_variants.get(phase, "default"))


def resolve_graph_steps(
    execution_plan: ExecutionPlan,
    graph_spec: GraphSpec,
    implementations: StepImplementationRegistry,
    metadata: dict[str, dict[str, StepMetadata]] | None = None,
) -> dict[str, ResolvedStep]:
    resolved_steps: dict[str, ResolvedStep] = {}
    step_metadata = metadata or DEFAULT_STEP_METADATA
    for node in graph_spec.nodes:
        variant = node.variant or resolve_phase_variant(execution_plan, node.phase)
        if variant == "default":
            variant = resolve_phase_variant(execution_plan, node.phase)
        phase_metadata = step_metadata.get(node.phase, {})
        phase_implementations = implementations.get(node.phase, {})
        step_meta = phase_metadata.get(variant)
        handler = phase_implementations.get(variant)
        if step_meta is None or handler is None:
            raise KeyError(
                f"Unsupported phase variant '{variant}' for phase '{node.phase}'"
            )
        resolved_steps[node.node_id] = ResolvedStep(
            node_id=node.node_id,
            phase=node.phase,
            variant=variant,
            step_name=step_meta.step_name,
            node_name=node.node_id or step_meta.default_node_name,
            handler=handler,
            emits_reply=step_meta.emits_reply,
        )
    return resolved_steps


def _unconditional_predecessors(graph_spec: GraphSpec) -> dict[str, set[str]]:
    predecessors: dict[str, set[str]] = defaultdict(set)
    for source, target in graph_spec.edges:
        predecessors[target].add(source)
    return predecessors


def _unconditional_successors(graph_spec: GraphSpec) -> dict[str, list[str]]:
    successors: dict[str, list[str]] = defaultdict(list)
    for source, target in graph_spec.edges:
        successors[source].append(target)
    return successors


def _conditional_edges_by_source(
    graph_spec: GraphSpec,
) -> dict[str, ConditionalEdgeSpec]:
    return {edge.source_node_id: edge for edge in graph_spec.conditional_edges}


def _conditional_incoming_sources(graph_spec: GraphSpec) -> dict[str, set[str]]:
    incoming: dict[str, set[str]] = defaultdict(set)
    for edge in graph_spec.conditional_edges:
        for target in edge.branches.values():
            if target != END_TARGET:
                incoming[target].add(edge.source_node_id)
        if edge.default_target_node_id and edge.default_target_node_id != END_TARGET:
            incoming[edge.default_target_node_id].add(edge.source_node_id)
    return incoming


def _active_node_ids(
    *,
    graph_spec: GraphSpec,
    selected_branch_targets: dict[str, str],
    unconditional_successors: dict[str, list[str]],
) -> set[str]:
    conditional_edges_by_source = _conditional_edges_by_source(graph_spec)
    active: set[str] = set()
    queue: deque[str] = deque(graph_spec.entry_node_ids)

    while queue:
        node_id = queue.popleft()
        if node_id in active:
            continue
        active.add(node_id)
        for target in unconditional_successors.get(node_id, []):
            if target not in active:
                queue.append(target)
        conditional_edge = conditional_edges_by_source.get(node_id)
        if conditional_edge is None:
            continue
        target = selected_branch_targets.get(node_id)
        if target and target != END_TARGET and target not in active:
            queue.append(target)

    return active


def topologically_order_steps(
    graph_spec: GraphSpec,
    resolved_steps: dict[str, ResolvedStep],
) -> list[ResolvedStep]:
    indegree: dict[str, int] = {node.node_id: 0 for node in graph_spec.nodes}
    adjacency: dict[str, list[str]] = defaultdict(list)
    for source, target in graph_spec.edges:
        adjacency[source].append(target)
        indegree[target] = indegree.get(target, 0) + 1

    queue: deque[str] = deque(
        node.node_id for node in graph_spec.nodes if indegree.get(node.node_id, 0) == 0
    )
    ordered: list[ResolvedStep] = []
    while queue:
        node_id = queue.popleft()
        ordered.append(resolved_steps[node_id])
        for target in adjacency.get(node_id, []):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)

    if len(ordered) != len(graph_spec.nodes):
        raise ValueError("Graph spec contains a cycle or disconnected node resolution failure")
    return ordered


def resolve_all_steps(
    execution_plan: ExecutionPlan,
    implementations: StepImplementationRegistry,
    metadata: dict[str, dict[str, StepMetadata]] | None = None,
) -> list[ResolvedStep]:
    resolved_steps = resolve_graph_steps(
        execution_plan,
        execution_plan.graph_spec,
        implementations,
        metadata,
    )
    return topologically_order_steps(execution_plan.graph_spec, resolved_steps)


def _merge_parallel_updates(
    state: dict[str, Any],
    updates_list: list[dict[str, Any]],
) -> dict[str, Any]:
    merged_state = dict(state)
    merged_dict_keys = {"step_attachments", "step_durations_ms"}
    batch_scalar_updates: dict[str, Any] = {}

    for updates in updates_list:
        for key, value in updates.items():
            if key in merged_dict_keys:
                existing = dict(merged_state.get(key, {}))
                existing.update(dict(value or {}))
                merged_state[key] = existing
                continue
            if key in batch_scalar_updates and batch_scalar_updates[key] != value:
                raise ValueError(f"Parallel nodes produced conflicting updates for key '{key}'")
            batch_scalar_updates[key] = value

    merged_state.update(batch_scalar_updates)
    return merged_state


def _node_is_ready(
    node_id: str,
    *,
    graph_spec: GraphSpec,
    active_node_ids: set[str],
    completed_node_ids: set[str],
    selected_branch_targets: dict[str, str],
    unconditional_predecessors: dict[str, set[str]],
    conditional_incoming_sources: dict[str, set[str]],
) -> bool:
    if node_id not in active_node_ids:
        return False
    if node_id in completed_node_ids:
        return False
    active_unconditional_predecessors = unconditional_predecessors.get(node_id, set()) & active_node_ids
    active_conditional_sources = conditional_incoming_sources.get(node_id, set()) & active_node_ids
    if not active_unconditional_predecessors and not active_conditional_sources:
        return node_id in graph_spec.entry_node_ids
    if not active_unconditional_predecessors.issubset(completed_node_ids):
        return False
    if not active_conditional_sources:
        return True
    return any(selected_branch_targets.get(source) == node_id for source in active_conditional_sources)


def _run_ready_batch(
    *,
    state: dict[str, Any],
    ready_node_ids: list[str],
    resolved_steps: dict[str, ResolvedStep],
    wrap_step: Callable[[str, StepHandler], StepHandler],
) -> dict[str, Any]:
    ordered_steps = [resolved_steps[node_id] for node_id in ready_node_ids]
    if len(ordered_steps) == 1:
        updates = [
            wrap_step(ordered_steps[0].node_name, ordered_steps[0].handler)(state)
        ]
        return _merge_parallel_updates(state, updates)

    with ThreadPoolExecutor(max_workers=len(ordered_steps)) as executor:
        futures = [
            executor.submit(wrap_step(step.node_name, step.handler), state)
            for step in ordered_steps
        ]
        updates = [future.result() for future in futures]
    return _merge_parallel_updates(state, updates)


def _next_ready_nodes(
    *,
    graph_spec: GraphSpec,
    ordered_node_ids: list[str],
    active_node_ids: set[str],
    completed_node_ids: set[str],
    selected_branch_targets: dict[str, str],
    unconditional_predecessors: dict[str, set[str]],
    conditional_incoming_sources: dict[str, set[str]],
) -> list[str]:
    return [
        node_id
        for node_id in ordered_node_ids
        if _node_is_ready(
            node_id,
            graph_spec=graph_spec,
            active_node_ids=active_node_ids,
            completed_node_ids=completed_node_ids,
            selected_branch_targets=selected_branch_targets,
            unconditional_predecessors=unconditional_predecessors,
            conditional_incoming_sources=conditional_incoming_sources,
        )
    ]


def execute_graph_until_reply_and_finalize(
    *,
    initial_state: dict[str, Any],
    execution_plan: ExecutionPlan,
    implementations: StepImplementationRegistry,
    wrap_step: Callable[[str, StepHandler], StepHandler],
    delivery_waiter: Callable[[], bool],
    emit_reply: Callable[[str], None],
    metadata: dict[str, dict[str, StepMetadata]] | None = None,
) -> dict[str, Any]:
    graph_spec = execution_plan.graph_spec
    resolved_steps = resolve_graph_steps(
        execution_plan,
        graph_spec,
        implementations,
        metadata,
    )
    ordered_node_ids = [node.node_id for node in graph_spec.nodes]
    conditional_edges_by_source = _conditional_edges_by_source(graph_spec)
    unconditional_predecessors = _unconditional_predecessors(graph_spec)
    unconditional_successors = _unconditional_successors(graph_spec)
    conditional_incoming_sources = _conditional_incoming_sources(graph_spec)
    completed_node_ids: set[str] = set()
    selected_branch_targets: dict[str, str] = {}
    state = dict(initial_state)
    reply_emitted = False

    while True:
        active_node_ids = _active_node_ids(
            graph_spec=graph_spec,
            selected_branch_targets=selected_branch_targets,
            unconditional_successors=unconditional_successors,
        )
        ready_node_ids = _next_ready_nodes(
            graph_spec=graph_spec,
            ordered_node_ids=ordered_node_ids,
            active_node_ids=active_node_ids,
            completed_node_ids=completed_node_ids,
            selected_branch_targets=selected_branch_targets,
            unconditional_predecessors=unconditional_predecessors,
            conditional_incoming_sources=conditional_incoming_sources,
        )
        if not ready_node_ids:
            break

        state = _run_ready_batch(
            state=state,
            ready_node_ids=ready_node_ids,
            resolved_steps=resolved_steps,
            wrap_step=wrap_step,
        )
        completed_node_ids.update(ready_node_ids)

        for node_id in ready_node_ids:
            conditional_edge = conditional_edges_by_source.get(node_id)
            if conditional_edge is None:
                continue
            branch_value = str(state.get(conditional_edge.condition_key, ""))
            target = conditional_edge.branches.get(
                branch_value,
                conditional_edge.default_target_node_id,
            )
            if target and target != END_TARGET:
                selected_branch_targets[node_id] = target
            elif node_id in selected_branch_targets:
                selected_branch_targets.pop(node_id, None)

        emitted_in_batch = [
            resolved_steps[node_id]
            for node_id in ready_node_ids
            if resolved_steps[node_id].emits_reply
        ]
        if emitted_in_batch and not reply_emitted:
            reply_emitted = True
            emit_reply(str(state.get("reply", "")))
            state = {**state, "assistant_reply_sent": bool(delivery_waiter())}

    return state


def build_graph_for_execution_plan(
    *,
    state_type: type[Any],
    execution_plan: ExecutionPlan,
    implementations: StepImplementationRegistry,
    wrap_step: Callable[[str, StepHandler], StepHandler],
    metadata: dict[str, dict[str, StepMetadata]] | None = None,
):
    graph = StateGraph(state_type)
    resolved_steps = resolve_graph_steps(
        execution_plan,
        execution_plan.graph_spec,
        implementations,
        metadata,
    )
    conditional_edges_by_source = _conditional_edges_by_source(execution_plan.graph_spec)
    for resolved_step in resolved_steps.values():
        graph.add_node(
            resolved_step.node_name,
            wrap_step(resolved_step.node_name, resolved_step.handler),
        )

    for entry_node_id in execution_plan.graph_spec.entry_node_ids:
        graph.add_edge(START, entry_node_id)
    for source, target in execution_plan.graph_spec.edges:
        graph.add_edge(source, target)
    for conditional_edge in execution_plan.graph_spec.conditional_edges:
        branches = dict(conditional_edge.branches)
        if conditional_edge.default_target_node_id is not None:
            branches.setdefault("__default__", conditional_edge.default_target_node_id)

        def _router(state: dict[str, Any], *, condition_key: str = conditional_edge.condition_key) -> str:
            value = str(state.get(condition_key, ""))
            return value or "__default__"

        target_map = {
            key: (END if value == END_TARGET else value)
            for key, value in branches.items()
        }
        graph.add_conditional_edges(conditional_edge.source_node_id, _router, target_map)
    for exit_node_id in execution_plan.graph_spec.exit_node_ids:
        if exit_node_id not in conditional_edges_by_source:
            graph.add_edge(exit_node_id, END)
    return graph.compile()
