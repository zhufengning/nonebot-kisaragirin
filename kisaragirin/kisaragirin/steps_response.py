from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .prompts import MEMORY_JSON_INSTRUCTION
from .reply_lite_checks import DEFAULT_LITE_REPLY_CHECKERS


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
    attempt = int(state.get("reply_lite_attempt", 0) or 0) + 1
    retry_feedback = str(state.get("reply_lite_retry_feedback", "") or "").strip()
    previous_reply = str(state.get("reply", "") or "").strip()
    reply_input = state["working_text"]
    if retry_feedback and previous_reply and previous_reply != "bot选择沉默":
        rejected_reply = f"{previous_reply}\n{retry_feedback}".strip()
        reply_input = (
            f"{state['working_text']}\n\n"
            "[上一版回复及报错]\n"
            f"{rejected_reply}\n\n"
            "上一版回复没有通过用语检查。请保留原本想回应的话题，但必须逐条修复这些报错，"
            "重新生成一条新的最终回复。不要解释修改过程，不要引用报错内容。"
        )

    model = _reply_model(agent, lite=True)
    reply_msg = model.invoke(
        [
            SystemMessage(content=agent._system_prompt("reply_lite")),
            HumanMessage(content=reply_input),
        ]
    )
    reply_text = agent._message_to_text(reply_msg.content)
    attachment = f"[REPLY_LITE][attempt={attempt}]\n" + reply_text
    agent._log_step_debug(state, "reply_lite", attachment)
    return {
        "reply": reply_text,
        "reply_lite_attempt": attempt,
        "step_attachments": agent._set_attachment(
            state,
            f"reply_lite[{attempt}]",
            attachment,
        ),
    }


def run_reply_lite_check(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    attempt = int(state.get("reply_lite_attempt", 0) or 0)
    reply_text = str(state.get("reply", "") or "").strip()
    conversation_id = str(state.get("conversation_id", "?"))
    route_id = str(state.get("active_route_id", "") or "unknown")

    if not reply_text:
        reply_text = "bot选择沉默"

    if reply_text == "bot选择沉默":
        attachment = (
            f"[REPLY_LITE_CHECK][attempt={attempt}]\n"
            "result=pass\n"
            "skipped_reason=reply_is_silence"
        )
        agent._log_info(
            "[LITE-CHECK][conversation=%s][route=%s][attempt=%s] skipped reply_is_silence",
            conversation_id,
            route_id,
            attempt,
        )
        agent._log_step_debug(state, "reply_lite_check", attachment)
        return {
            "reply": reply_text,
            "reply_lite_check_result": "pass",
            "reply_lite_retry_feedback": "",
            "step_attachments": agent._set_attachment(
                state,
                f"reply_lite_check[{attempt}]",
                attachment,
            ),
        }

    diagnostics_list: list[str] = []
    for checker in DEFAULT_LITE_REPLY_CHECKERS:
        result = checker(reply_text)
        if result.passed:
            agent._log_info(
                "[LITE-CHECK][conversation=%s][route=%s][attempt=%s][checker=%s] pass",
                conversation_id,
                route_id,
                attempt,
                result.checker_name,
            )
            continue
        diagnostics_list.append(result.diagnostics)
        agent._log_info(
            "[LITE-CHECK][conversation=%s][route=%s][attempt=%s][checker=%s] fail\n%s",
            conversation_id,
            route_id,
            attempt,
            result.checker_name,
            result.diagnostics,
        )

    if not diagnostics_list:
        attachment = (
            f"[REPLY_LITE_CHECK][attempt={attempt}]\n"
            "result=pass\n"
            "failed_checker_count=0"
        )
        agent._log_info(
            "[LITE-CHECK][conversation=%s][route=%s][attempt=%s] result=pass failed_checker_count=0",
            conversation_id,
            route_id,
            attempt,
        )
        agent._log_step_debug(state, "reply_lite_check", attachment)
        return {
            "reply_lite_check_result": "pass",
            "reply_lite_retry_feedback": "",
            "step_attachments": agent._set_attachment(
                state,
                f"reply_lite_check[{attempt}]",
                attachment,
            ),
        }

    retry_feedback = "\n\n".join(diagnostics_list).strip()
    check_result = "cancel" if attempt >= 3 else "retry"
    attachment = (
        f"[REPLY_LITE_CHECK][attempt={attempt}]\n"
        f"result={check_result}\n"
        f"failed_checker_count={len(diagnostics_list)}\n"
        f"feedback:\n{retry_feedback}"
    )
    agent._log_info(
        "[LITE-CHECK][conversation=%s][route=%s][attempt=%s] result=%s failed_checker_count=%s",
        conversation_id,
        route_id,
        attempt,
        check_result,
        len(diagnostics_list),
    )
    agent._log_step_debug(state, "reply_lite_check", attachment)
    return {
        "reply": "bot选择沉默" if check_result == "cancel" else reply_text,
        "reply_lite_check_result": check_result,
        "reply_lite_retry_feedback": retry_feedback if check_result == "retry" else "",
        "step_attachments": agent._set_attachment(
            state,
            f"reply_lite_check[{attempt}]",
            attachment,
        ),
    }


def run_memory_gate(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    delivered_outputs = state.get("delivered_outputs") or []
    should_update_memory = bool(delivered_outputs)
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

    delivered_outputs = state.get("delivered_outputs") or []
    delivered_reply_blocks: list[str] = []
    for index, output in enumerate(delivered_outputs, start=1):
        route_id = getattr(output, "route_id", "") or "unknown"
        content = getattr(output, "content", "") or ""
        delivered_reply_blocks.append(f"{index}. route={route_id}\n{content}")
    delivered_reply_text = "\n\n".join(delivered_reply_blocks).strip()

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
                    "[THIS-TURN-REPLIES]\n"
                    f"{delivered_reply_text or '(empty)'}"
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
        assistant_reply=delivered_reply_text,
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
