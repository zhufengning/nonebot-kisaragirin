from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from .routing import ExecutionPlan

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


def graph_node_name(phase: str, variant: str) -> str:
    return f"phase_{phase}__{variant}"


def resolve_steps_for_phases(
    execution_plan: ExecutionPlan,
    phases: tuple[str, ...],
    implementations: StepImplementationRegistry,
    metadata: dict[str, dict[str, StepMetadata]] | None = None,
) -> list[ResolvedStep]:
    resolved_steps: list[ResolvedStep] = []
    step_metadata = metadata or DEFAULT_STEP_METADATA
    for phase in phases:
        variant = resolve_phase_variant(execution_plan, phase)
        phase_metadata = step_metadata.get(phase, {})
        phase_implementations = implementations.get(phase, {})
        step_meta = phase_metadata.get(variant)
        handler = phase_implementations.get(variant)
        if step_meta is None or handler is None:
            raise KeyError(
                f"Unsupported phase variant '{variant}' for phase '{phase}'"
            )
        resolved_steps.append(
            ResolvedStep(
                phase=phase,
                variant=variant,
                step_name=step_meta.step_name,
                node_name=graph_node_name(phase, variant)
                or step_meta.default_node_name,
                handler=handler,
                emits_reply=step_meta.emits_reply,
            )
        )
    return resolved_steps


def resolve_all_steps(
    execution_plan: ExecutionPlan,
    implementations: StepImplementationRegistry,
    metadata: dict[str, dict[str, StepMetadata]] | None = None,
) -> list[ResolvedStep]:
    return [
        *resolve_steps_for_phases(
            execution_plan,
            execution_plan.shared_prelude_phases,
            implementations,
            metadata,
        ),
        *resolve_steps_for_phases(
            execution_plan,
            execution_plan.route_middle_phases,
            implementations,
            metadata,
        ),
        *resolve_steps_for_phases(
            execution_plan,
            execution_plan.shared_finalize_phases,
            implementations,
            metadata,
        ),
    ]


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
    resolved_steps = resolve_all_steps(
        execution_plan,
        implementations,
        metadata,
    )
    for resolved_step in resolved_steps:
        graph.add_node(
            resolved_step.node_name,
            wrap_step(resolved_step.step_name, resolved_step.handler),
        )

    graph.add_edge(START, resolved_steps[0].node_name)
    for current_step, next_step in zip(resolved_steps, resolved_steps[1:]):
        graph.add_edge(current_step.node_name, next_step.node_name)
    graph.add_edge(resolved_steps[-1].node_name, END)
    return graph.compile()
