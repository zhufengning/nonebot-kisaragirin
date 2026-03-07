from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_ROUTE_ID = "default"


@dataclass(slots=True, frozen=True)
class GraphNodeSpec:
    node_id: str
    phase: str
    variant: str = "default"


@dataclass(slots=True, frozen=True)
class GraphSpec:
    nodes: tuple[GraphNodeSpec, ...]
    edges: tuple[tuple[str, str], ...]
    entry_node_ids: tuple[str, ...]
    exit_node_ids: tuple[str, ...]


DEFAULT_SHARED_PRELUDE_GRAPH = GraphSpec(
    nodes=(
        GraphNodeSpec(node_id="prepare", phase="prepare"),
        GraphNodeSpec(node_id="url", phase="url"),
        GraphNodeSpec(node_id="vision", phase="vision"),
    ),
    edges=(
        ("prepare", "url"),
        ("url", "vision"),
    ),
    entry_node_ids=("prepare",),
    exit_node_ids=("vision",),
)

DEFAULT_ROUTE_MIDDLE_GRAPH = GraphSpec(
    nodes=(
        GraphNodeSpec(node_id="tools", phase="tools"),
        GraphNodeSpec(node_id="reply", phase="reply"),
    ),
    edges=(("tools", "reply"),),
    entry_node_ids=("tools",),
    exit_node_ids=("reply",),
)

DEFAULT_SHARED_FINALIZE_GRAPH = GraphSpec(
    nodes=(GraphNodeSpec(node_id="memory", phase="memory"),),
    edges=(),
    entry_node_ids=("memory",),
    exit_node_ids=("memory",),
)


@dataclass(slots=True, frozen=True)
class RouteDecision:
    route_id: str
    shared_prelude_graph: GraphSpec
    route_middle_graph: GraphSpec
    shared_finalize_graph: GraphSpec
    phase_variants: dict[str, str] = field(default_factory=dict)
    reason: str = ""


@dataclass(slots=True, frozen=True)
class ExecutionPlan:
    route_id: str
    shared_prelude_graph: GraphSpec
    route_middle_graph: GraphSpec
    shared_finalize_graph: GraphSpec
    graph_spec: GraphSpec
    phase_variants: dict[str, str] = field(default_factory=dict)


def build_default_route_decision() -> RouteDecision:
    return RouteDecision(
        route_id=DEFAULT_ROUTE_ID,
        shared_prelude_graph=DEFAULT_SHARED_PRELUDE_GRAPH,
        route_middle_graph=DEFAULT_ROUTE_MIDDLE_GRAPH,
        shared_finalize_graph=DEFAULT_SHARED_FINALIZE_GRAPH,
        phase_variants={
            "tools": DEFAULT_ROUTE_ID,
            "reply": DEFAULT_ROUTE_ID,
        },
        reason="default-route",
    )


def compose_graph_segments(*segments: GraphSpec) -> GraphSpec:
    active_segments = [segment for segment in segments if segment.nodes]
    if not active_segments:
        return GraphSpec(nodes=(), edges=(), entry_node_ids=(), exit_node_ids=())

    nodes: list[GraphNodeSpec] = []
    edges: list[tuple[str, str]] = []
    for index, segment in enumerate(active_segments):
        nodes.extend(segment.nodes)
        edges.extend(segment.edges)
        if index == 0:
            continue
        previous = active_segments[index - 1]
        for exit_node_id in previous.exit_node_ids:
            for entry_node_id in segment.entry_node_ids:
                edges.append((exit_node_id, entry_node_id))

    return GraphSpec(
        nodes=tuple(nodes),
        edges=tuple(edges),
        entry_node_ids=tuple(active_segments[0].entry_node_ids),
        exit_node_ids=tuple(active_segments[-1].exit_node_ids),
    )


def build_execution_plan(decision: RouteDecision) -> ExecutionPlan:
    graph_spec = compose_graph_segments(
        decision.shared_prelude_graph,
        decision.route_middle_graph,
        decision.shared_finalize_graph,
    )
    return ExecutionPlan(
        route_id=decision.route_id,
        shared_prelude_graph=decision.shared_prelude_graph,
        route_middle_graph=decision.route_middle_graph,
        shared_finalize_graph=decision.shared_finalize_graph,
        graph_spec=graph_spec,
        phase_variants=dict(decision.phase_variants),
    )
