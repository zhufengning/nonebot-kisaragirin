from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_ROUTE_ID = "default"
DEFAULT_SHARED_PRELUDE_PHASES = (
    "prepare",
    "url",
    "vision",
)
DEFAULT_ROUTE_MIDDLE_PHASES = (
    "tools",
    "reply",
)
DEFAULT_SHARED_FINALIZE_PHASES = ("memory",)
DEFAULT_PHASE_ORDER = (
    *DEFAULT_SHARED_PRELUDE_PHASES,
    *DEFAULT_ROUTE_MIDDLE_PHASES,
    *DEFAULT_SHARED_FINALIZE_PHASES,
)


@dataclass(slots=True, frozen=True)
class RouteDecision:
    route_id: str
    enabled_phases: tuple[str, ...]
    phase_variants: dict[str, str] = field(default_factory=dict)
    reason: str = ""


@dataclass(slots=True, frozen=True)
class ExecutionPlan:
    route_id: str
    shared_prelude_phases: tuple[str, ...]
    route_middle_phases: tuple[str, ...]
    shared_finalize_phases: tuple[str, ...]
    phase_order: tuple[str, ...]
    phase_variants: dict[str, str] = field(default_factory=dict)


def build_default_route_decision() -> RouteDecision:
    return RouteDecision(
        route_id=DEFAULT_ROUTE_ID,
        enabled_phases=DEFAULT_PHASE_ORDER,
        phase_variants={
            "tools": DEFAULT_ROUTE_ID,
            "reply": DEFAULT_ROUTE_ID,
        },
        reason="default-route",
    )


def _enabled_phases_subset(
    enabled_phases: tuple[str, ...],
    allowed_phases: tuple[str, ...],
) -> tuple[str, ...]:
    allowed = set(allowed_phases)
    return tuple(phase for phase in enabled_phases if phase in allowed)


def build_execution_plan(decision: RouteDecision) -> ExecutionPlan:
    shared_prelude_phases = _enabled_phases_subset(
        decision.enabled_phases,
        DEFAULT_SHARED_PRELUDE_PHASES,
    )
    route_middle_phases = _enabled_phases_subset(
        decision.enabled_phases,
        DEFAULT_ROUTE_MIDDLE_PHASES,
    )
    shared_finalize_phases = _enabled_phases_subset(
        decision.enabled_phases,
        DEFAULT_SHARED_FINALIZE_PHASES,
    )
    phase_order = (
        *shared_prelude_phases,
        *route_middle_phases,
        *shared_finalize_phases,
    )
    return ExecutionPlan(
        route_id=decision.route_id,
        shared_prelude_phases=shared_prelude_phases,
        route_middle_phases=route_middle_phases,
        shared_finalize_phases=shared_finalize_phases,
        phase_order=phase_order,
        phase_variants=dict(decision.phase_variants),
    )
