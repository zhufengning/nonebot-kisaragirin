from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .prompts import ROUTE_PROMPT
from .routing import DEFAULT_ROUTE_ID, LITE_CHAT_ROUTE_ID


def run_step_route(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    route_model_id = str(getattr(agent._config.step_models, "route", "") or "").strip()
    model = agent._model(route_model_id or agent._config.step_models.reply)
    route_msg = model.invoke(
        [
            SystemMessage(content=ROUTE_PROMPT),
            HumanMessage(content=state["working_text"]),
        ]
    )
    route_choice = agent._message_to_text(route_msg.content).strip().lower()
    if route_choice not in {DEFAULT_ROUTE_ID, LITE_CHAT_ROUTE_ID}:
        route_choice = DEFAULT_ROUTE_ID
    attachment = f"[STEP-R-ROUTE]\nroute_choice={route_choice}"
    agent._log_step_debug(state, "STEP-R", attachment)
    return {
        "route_choice": route_choice,
        "step_attachments": agent._set_attachment(state, "STEP-R", attachment),
    }
