from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .prompts import MEMORY_JSON_INSTRUCTION


def _reply_model(agent: Any, *, lite: bool = False):
    if lite:
        lite_model_id = str(getattr(agent._config.step_models, "lite_reply", "") or "").strip()
        if lite_model_id:
            return agent._model(lite_model_id)
    return agent._model(agent._config.step_models.reply)


def _run_reply(agent: Any, state: dict[str, Any], *, step_name: str = "reply") -> dict[str, Any]:
    model = _reply_model(agent)
    reply_msg = model.invoke(
        [
            SystemMessage(content=agent._system_prompt("reply")),
            HumanMessage(content=state["working_text"]),
        ]
    )
    reply_text = agent._message_to_text(reply_msg.content)
    attachment = f"[{step_name.upper()}]\n" + reply_text
    agent._log_step_debug(state, step_name, attachment)
    return {
        "reply": reply_text,
        "step_attachments": agent._set_attachment(state, step_name, attachment),
    }


def run_reply(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    return _run_reply(agent, state, step_name="reply")


def run_reply_lite(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    model = _reply_model(agent, lite=True)
    reply_msg = model.invoke(
        [
            SystemMessage(content=agent._system_prompt("reply_lite")),
            HumanMessage(content=state["working_text"]),
        ]
    )
    reply_text = agent._message_to_text(reply_msg.content)
    attachment = "[REPLY_LITE]\n" + reply_text
    agent._log_step_debug(state, "reply_lite", attachment)
    return {
        "reply": reply_text,
        "step_attachments": agent._set_attachment(state, "reply_lite", attachment),
    }


def run_memory_gate(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    should_update_memory = bool(state.get("assistant_reply_sent", True)) and bool(
        str(state.get("reply", "")).strip()
    ) and str(state.get("reply", "")).strip() != "bot选择沉默"
    memory_gate_result = "update" if should_update_memory else "skip"
    attachment = (
        "[MEMORY-GATE]\n"
        f"memory_gate_result={memory_gate_result}"
    )
    agent._log_step_debug(state, "memory_gate", attachment)
    return {
        "memory_gate_result": memory_gate_result,
        "step_attachments": agent._set_attachment(state, "memory_gate", attachment),
    }


def run_memory(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    if str(state.get("memory_gate_result", "update")) != "update":
        attachment = (
            "[MEMORY-UPDATE]\n"
            "long_term_memory_updated=false\n"
            "short_term_memory_appended=none\n"
            f"skipped_reason={state.get('memory_gate_result', 'skip')}"
        )
        agent._log_step_debug(state, "memory", attachment)
        return {
            "step_attachments": agent._set_attachment(state, "memory", attachment),
        }

    memory_model = agent._model(agent._config.step_models.memory)

    msg = memory_model.invoke(
        [
            SystemMessage(content=agent._system_prompt("memory")),
            HumanMessage(
                content=(
                    f"{MEMORY_JSON_INSTRUCTION}\n\n"
                    "[PREVIOUS-LONG-TERM-MEMORY]\n"
                    f"{state.get('long_term_memory') or '(empty)'}\n\n"
                    "[THIS-TURN-ENRICHED-INPUT]\n"
                    f"{state['working_text']}\n\n"
                    "[THIS-TURN-REPLY]\n"
                    f"{state.get('reply', '')}"
                )
            ),
        ]
    )

    parsed = agent._parse_memory_json(agent._message_to_text(msg.content))
    new_long_term = agent._normalize_memory_text(
        parsed.get("long_term_memory"),
        fallback=state.get("long_term_memory", ""),
    )
    memory_compacted = False
    if len(new_long_term) > 2000:
        compact_msg = memory_model.invoke(
            [
                SystemMessage(content=agent._system_prompt("memory")),
                HumanMessage(
                    content=(
                        f"{MEMORY_JSON_INSTRUCTION}\n\n"
                        "你的记忆太长了，需要精简到2000字符以内。\n\n"
                        "[CURRENT-LONG-TERM-MEMORY]\n"
                        f"{new_long_term}"
                    )
                ),
            ]
        )
        compact_parsed = agent._parse_memory_json(
            agent._message_to_text(compact_msg.content)
        )
        new_long_term = agent._normalize_memory_text(
            compact_parsed.get("long_term_memory"),
            fallback=new_long_term,
        )
        if len(new_long_term) > 2000:
            new_long_term = new_long_term[:2000]
        memory_compacted = True
    agent._memory_store.persist_turn(
        conversation_id=state["conversation_id"],
        long_term_memory=new_long_term,
        user_message=str(state.get("user_message", "")),
        assistant_reply=state.get("reply", ""),
        user_image_hashes=state.get("image_hashes") or [],
    )

    attachment = (
        "[MEMORY-UPDATE]\n"
        "long_term_memory_updated=true\n"
        f"long_term_memory_compacted={'true' if memory_compacted else 'false'}\n"
        "short_term_memory_appended=user+assistant"
    )
    agent._log_step_debug(
        state,
        "memory",
        attachment + f"\nupdated_long_term_memory:\n{new_long_term}",
    )
    return {
        "long_term_memory": new_long_term,
        "step_attachments": agent._set_attachment(state, "memory", attachment),
    }
