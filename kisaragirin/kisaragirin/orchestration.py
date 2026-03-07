from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from .routing import ExecutionPlan, GraphNodeSpec, GraphSpec

StepHandler = Callable[[Any], Any]
StepImplementationRegistry = dict[str, dict[str, StepHandler]]


@dataclass(slots=True, frozen=True)
class StepMetadata:
    step_name: str
    default_node_name: str
    emits_reply: bool = False


DEFAULT_STEP_METADATA: dict[str, dict[str, StepMetadata]] = {
    "prepare": {
        "default": StepMetadata("STEP-0", "step0_prepare"),
    },
    "url": {
        "default": StepMetadata("STEP-1", "step1_urls"),
    },
    "vision": {
        "default": StepMetadata("STEP-2", "step2_vision"),
    },
    "tools": {
        "default": StepMetadata("STEP-3", "step3_tools"),
    },
    "reply": {
        "default": StepMetadata("STEP-4", "step4_reply", emits_reply=True),
    },
    "memory": {
        "default": StepMetadata("STEP-5", "step5_memory"),
    },
}


@dataclass(slots=True, frozen=True)
class ResolvedStep:
    phase: str
    variant: str
    step_name: str
    node_name: str
    handler: StepHandler
    emits_reply: bool = False


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
            phase=node.phase,
            variant=variant,
            step_name=step_meta.step_name,
            node_name=node.node_id or step_meta.default_node_name,
            handler=handler,
            emits_reply=step_meta.emits_reply,
        )
    return resolved_steps


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


def split_resolved_steps_for_reply_first(
    resolved_steps: list[ResolvedStep],
) -> tuple[list[ResolvedStep], list[ResolvedStep]]:
    emit_index = next(
        (index for index, step in enumerate(resolved_steps) if step.emits_reply),
        None,
    )
    if emit_index is None:
        raise ValueError("Execution plan does not define any reply-emitting step")
    reply_path_steps = resolved_steps[: emit_index + 1]
    finalize_steps = resolved_steps[emit_index + 1 :]
    return reply_path_steps, finalize_steps


def resolve_reply_first_step_groups(
    execution_plan: ExecutionPlan,
    implementations: StepImplementationRegistry,
    metadata: dict[str, dict[str, StepMetadata]] | None = None,
) -> tuple[list[ResolvedStep], list[ResolvedStep]]:
    resolved_steps = resolve_all_steps(
        execution_plan,
        implementations,
        metadata,
    )
    return split_resolved_steps_for_reply_first(resolved_steps)


def execute_resolved_steps(
    *,
    state: Any,
    resolved_steps: list[ResolvedStep],
    wrap_step: Callable[[str, StepHandler], StepHandler],
) -> Any:
    current_state = state
    for resolved_step in resolved_steps:
        updates = wrap_step(resolved_step.step_name, resolved_step.handler)(current_state)
        current_state = {**current_state, **updates}
    return current_state


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
    for resolved_step in resolved_steps.values():
        graph.add_node(
            resolved_step.node_name,
            wrap_step(resolved_step.step_name, resolved_step.handler),
        )

    for entry_node_id in execution_plan.graph_spec.entry_node_ids:
        graph.add_edge(START, entry_node_id)
    for source, target in execution_plan.graph_spec.edges:
        graph.add_edge(source, target)
    for exit_node_id in execution_plan.graph_spec.exit_node_ids:
        graph.add_edge(exit_node_id, END)
    return graph.compile()
