from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .prompts import ROUTE_PROMPT
def _build_route_input(agent: Any, state: dict[str, Any]) -> str:
    route_decision = state.get("route_decision")
    if route_decision is None:
        raise RuntimeError("missing route_decision for route step")

    blocks: list[str] = ["[ROUTE-OPTIONS]"]
    for index, route_id in enumerate(route_decision.route_ids, start=1):
        description = route_decision.route_descriptions.get(route_id, "").strip() or "(empty)"
        blocks.append(
            f"{index}. route_id={route_id}\n"
            f"[REQUIREMENT]\n{description}"
        )

    blocks.append("")
    blocks.append("[WORKING-CONTEXT]")
    blocks.append(str(state["working_text"]))
    return "\n\n".join(blocks)


def _parse_route_choices(
    text: str,
    *,
    allowed_route_ids: set[str],
) -> tuple[list[str], bool]:
    content = text.strip()
    if not content:
        return [], False

    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:].strip()

    routes: list[str] = []
    parsed_valid = False

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        raw_routes = parsed.get("routes")
        if isinstance(raw_routes, list):
            routes = [str(item) for item in raw_routes]
            parsed_valid = True
    elif isinstance(parsed, list):
        routes = [str(item) for item in parsed]
        parsed_valid = True
    else:
        normalized = content.lower()
        if normalized:
            routes = [normalized]
            parsed_valid = True

    normalized_routes: list[str] = []
    seen: set[str] = set()
    for route_id in routes:
        normalized_route_id = str(route_id).strip().lower()
        if normalized_route_id not in allowed_route_ids:
            continue
        if normalized_route_id in seen:
            continue
        seen.add(normalized_route_id)
        normalized_routes.append(normalized_route_id)
    if not normalized_routes and routes and parsed_valid:
        return [], False
    return normalized_routes, parsed_valid


def run_route(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    route_model_id = str(getattr(agent._config.step_models, "route", "") or "").strip()
    model = agent._model(route_model_id or agent._config.step_models.reply)
    route_msg = model.invoke(
        [
            SystemMessage(content=ROUTE_PROMPT),
            HumanMessage(content=_build_route_input(agent, state)),
        ]
    )

    route_decision = state.get("route_decision")
    if route_decision is None:
        raise RuntimeError("missing route_decision for route step")

    route_choices, parsed_valid = _parse_route_choices(
        agent._message_to_text(route_msg.content),
        allowed_route_ids=set(route_decision.route_ids),
    )
    if not parsed_valid:
        route_choices = list(route_decision.default_route_choices)

    attachment = (
        "[ROUTE]\n"
        f"route_choices={json.dumps(route_choices, ensure_ascii=False)}\n"
        f"parsed_valid={'true' if parsed_valid else 'false'}"
    )
    agent._log_step_debug(state, "route", attachment)
    return {
        "route_choices": route_choices,
        "step_attachments": agent._set_attachment(state, "route", attachment),
    }
