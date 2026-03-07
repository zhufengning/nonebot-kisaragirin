from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .prompts import MEMORY_JSON_INSTRUCTION


def run_step4_reply(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    model = agent._model(agent._config.step_models.reply)
    reply_msg = model.invoke(
        [
            SystemMessage(content=agent._system_prompt("reply")),
            HumanMessage(content=state["working_text"]),
        ]
    )
    reply_text = agent._message_to_text(reply_msg.content)
    attachment = "[STEP-4-REPLY]\n" + reply_text
    agent._log_step_debug(state, "STEP-4", attachment)
    return {
        "reply": reply_text,
        "step_attachments": agent._set_attachment(state, "STEP-4", attachment),
    }


def run_step5_memory(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    if not bool(state.get("assistant_reply_sent", True)):
        attachment = (
            "[STEP-5-MEMORY-UPDATE]\n"
            "long_term_memory_updated=false\n"
            "short_term_memory_appended=none\n"
            "skipped_reason=assistant_reply_not_sent"
        )
        agent._log_step_debug(state, "STEP-5", attachment)
        return {
            "step_attachments": agent._set_attachment(state, "STEP-5", attachment),
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
        "[STEP-5-MEMORY-UPDATE]\n"
        "long_term_memory_updated=true\n"
        f"long_term_memory_compacted={'true' if memory_compacted else 'false'}\n"
        "short_term_memory_appended=user+assistant"
    )
    agent._log_step_debug(
        state,
        "STEP-5",
        attachment + f"\nupdated_long_term_memory:\n{new_long_term}",
    )
    return {
        "long_term_memory": new_long_term,
        "step_attachments": agent._set_attachment(state, "STEP-5", attachment),
    }
