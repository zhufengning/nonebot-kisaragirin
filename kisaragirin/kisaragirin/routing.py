from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_ROUTE_ID = "default"
LITE_CHAT_ROUTE_ID = "lite_chat"
END_TARGET = "__END__"


@dataclass(slots=True, frozen=True)
class GraphNodeSpec:
    node_id: str
    phase: str
    variant: str = "default"


@dataclass(slots=True, frozen=True)
class ConditionalEdgeSpec:
    source_node_id: str
    condition_key: str
    branches: dict[str, str] = field(default_factory=dict)
    default_target_node_id: str | None = None


@dataclass(slots=True, frozen=True)
class GraphSpec:
    nodes: tuple[GraphNodeSpec, ...]
    edges: tuple[tuple[str, str], ...]
    entry_node_ids: tuple[str, ...]
    exit_node_ids: tuple[str, ...]
    conditional_edges: tuple[ConditionalEdgeSpec, ...] = ()


EMPTY_GRAPH = GraphSpec(
    nodes=(),
    edges=(),
    entry_node_ids=(),
    exit_node_ids=(),
    conditional_edges=(),
)


DEFAULT_SHARED_PRELUDE_GRAPH = GraphSpec(
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


DEFAULT_ROUTE_SELECTOR_GRAPH = GraphSpec(
    nodes=(
        GraphNodeSpec(node_id="route", phase="route"),
    ),
    edges=(),
    entry_node_ids=("route",),
    exit_node_ids=("route",),
)


DEFAULT_ROUTE_GRAPH = GraphSpec(
    nodes=(
        GraphNodeSpec(node_id="tools", phase="tools"),
        GraphNodeSpec(node_id="reply", phase="reply", variant="default"),
    ),
    edges=(("tools", "reply"),),
    entry_node_ids=("tools",),
    exit_node_ids=("reply",),
)


LITE_CHAT_ROUTE_GRAPH = GraphSpec(
    nodes=(
        GraphNodeSpec(node_id="reply_lite", phase="reply", variant="lite"),
    ),
    edges=(),
    entry_node_ids=("reply_lite",),
    exit_node_ids=("reply_lite",),
)


DEFAULT_SHARED_FINALIZE_GRAPH = GraphSpec(
    nodes=(
        GraphNodeSpec(node_id="memory_gate", phase="memory_gate"),
        GraphNodeSpec(node_id="memory", phase="memory"),
    ),
    edges=(),
    entry_node_ids=("memory_gate",),
    exit_node_ids=("memory",),
    conditional_edges=(
        ConditionalEdgeSpec(
            source_node_id="memory_gate",
            condition_key="memory_gate_result",
            branches={
                "update": "memory",
                "skip": END_TARGET,
            },
            default_target_node_id=END_TARGET,
        ),
    ),
)


@dataclass(slots=True, frozen=True)
class RouteDecision:
    route_id: str
    shared_prelude_graph: GraphSpec
    route_selector_graph: GraphSpec
    route_graphs: dict[str, GraphSpec]
    shared_finalize_graph: GraphSpec
    phase_variants_by_route: dict[str, dict[str, str]] = field(default_factory=dict)
    reason: str = ""


@dataclass(slots=True, frozen=True)
class ExecutionPlan:
    route_id: str
    shared_prelude_graph: GraphSpec
    route_selector_graph: GraphSpec
    route_graph: GraphSpec
    shared_finalize_graph: GraphSpec
    graph_spec: GraphSpec
    phase_variants: dict[str, str] = field(default_factory=dict)


def normalize_route_id(route_id: str) -> str:
    if route_id == LITE_CHAT_ROUTE_ID:
        return LITE_CHAT_ROUTE_ID
    return DEFAULT_ROUTE_ID


def build_default_route_decision() -> RouteDecision:
    return RouteDecision(
        route_id=DEFAULT_ROUTE_ID,
        shared_prelude_graph=DEFAULT_SHARED_PRELUDE_GRAPH,
        route_selector_graph=DEFAULT_ROUTE_SELECTOR_GRAPH,
        route_graphs={
            DEFAULT_ROUTE_ID: DEFAULT_ROUTE_GRAPH,
            LITE_CHAT_ROUTE_ID: LITE_CHAT_ROUTE_GRAPH,
        },
        shared_finalize_graph=DEFAULT_SHARED_FINALIZE_GRAPH,
        phase_variants_by_route={
            DEFAULT_ROUTE_ID: {
                "tools": DEFAULT_ROUTE_ID,
                "reply": DEFAULT_ROUTE_ID,
            },
            LITE_CHAT_ROUTE_ID: {
                "reply": "lite",
            },
        },
        reason="default-route",
    )


def compose_graph_segments(*segments: GraphSpec) -> GraphSpec:
    active_segments = [segment for segment in segments if segment.nodes]
    if not active_segments:
        return EMPTY_GRAPH

    nodes: list[GraphNodeSpec] = []
    edges: list[tuple[str, str]] = []
    conditional_edges: list[ConditionalEdgeSpec] = []
    for index, segment in enumerate(active_segments):
        nodes.extend(segment.nodes)
        edges.extend(segment.edges)
        conditional_edges.extend(segment.conditional_edges)
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
        conditional_edges=tuple(conditional_edges),
    )


def build_route_selection_plan(decision: RouteDecision) -> ExecutionPlan:
    graph_spec = compose_graph_segments(
        decision.shared_prelude_graph,
        decision.route_selector_graph,
    )
    return ExecutionPlan(
        route_id=decision.route_id,
        shared_prelude_graph=decision.shared_prelude_graph,
        route_selector_graph=decision.route_selector_graph,
        route_graph=EMPTY_GRAPH,
        shared_finalize_graph=EMPTY_GRAPH,
        graph_spec=graph_spec,
        phase_variants={},
    )


def build_execution_plan(
    decision: RouteDecision,
    route_id: str | None = None,
    *,
    include_prelude: bool = True,
    include_route_selector: bool = True,
    include_finalize: bool = True,
) -> ExecutionPlan:
    selected_route_id = normalize_route_id(route_id or decision.route_id)
    route_graph = decision.route_graphs.get(
        selected_route_id,
        decision.route_graphs[DEFAULT_ROUTE_ID],
    )
    graph_spec = compose_graph_segments(
        decision.shared_prelude_graph if include_prelude else EMPTY_GRAPH,
        decision.route_selector_graph if include_route_selector else EMPTY_GRAPH,
        route_graph,
        decision.shared_finalize_graph if include_finalize else EMPTY_GRAPH,
    )
    return ExecutionPlan(
        route_id=selected_route_id,
        shared_prelude_graph=decision.shared_prelude_graph,
        route_selector_graph=decision.route_selector_graph,
        route_graph=route_graph,
        shared_finalize_graph=decision.shared_finalize_graph,
        graph_spec=graph_spec,
        phase_variants=dict(decision.phase_variants_by_route.get(selected_route_id, {})),
    )
