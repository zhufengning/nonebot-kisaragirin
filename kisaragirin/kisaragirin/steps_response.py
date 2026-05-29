from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .message_types import Message, MessageSegment, messages_to_json
from .prompts import MEMORY_JSON_INSTRUCTION
from .reply_lite_checks import DEFAULT_LITE_REPLY_CHECKERS


def _reply_step(agent: Any, *, lite: bool = False) -> str:
    if lite:
        lite_model_id = str(getattr(agent._config.step_models, "lite_reply", "") or "").strip()
        if lite_model_id:
            return "lite_reply"
    return "reply"


def _run_reply(agent: Any, state: dict[str, Any], *, step_name: str = "reply") -> dict[str, Any]:
    messages = [
        SystemMessage(content=agent._system_prompt("reply")),
        HumanMessage(content=state["working_text"]),
    ]
    agent._log_model_messages(state, f"{step_name}.input_first", messages)
    reply_msg = agent._invoke_model(step_name, messages)
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

    step = _reply_step(agent, lite=True)
    messages = [
        SystemMessage(content=agent._system_prompt("reply_lite")),
        HumanMessage(content=reply_input),
    ]
    if attempt == 1:
        agent._log_model_messages(state, "reply_lite.input_first", messages)
    reply_msg = agent._invoke_model(step, messages)
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

    if not reply_text:
        reply_text = "bot选择沉默"

    if reply_text == "bot选择沉默":
        attachment = (
            f"[REPLY_LITE_CHECK][attempt={attempt}]\n"
            "result=pass\n"
            "retry_feedback:\n(none)\n"
            "skipped_reason=reply_is_silence"
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
        if not result.passed:
            diagnostics_list.append(result.diagnostics)

    if not diagnostics_list:
        attachment = (
            f"[REPLY_LITE_CHECK][attempt={attempt}]\n"
            "result=pass\n"
            "failed_checker_count=0\n"
            "retry_feedback:\n(none)"
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
        f"retry_feedback:\n{retry_feedback}"
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
    return {
        "memory_gate_result": memory_gate_result,
    }


def run_memory(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    should_update_memory = str(state.get("memory_gate_result", "update")) == "update"

    delivered_outputs = state.get("delivered_outputs") or []
    delivered_reply_blocks = [
        str(getattr(output, "content", "") or "").strip()
        for output in delivered_outputs
        if str(getattr(output, "content", "") or "").strip()
        and str(getattr(output, "content", "") or "").strip() != "bot选择沉默"
    ]
    delivered_reply_text = "\n\n".join(delivered_reply_blocks).strip()

    new_long_term = str(state.get("long_term_memory") or "")
    memory_compacted = False
    long_term_memory_updated = False
    memory_update_error = ""

    if should_update_memory:
        memory_input = (
            f"{MEMORY_JSON_INSTRUCTION}\n\n"
            "[PREVIOUS-LONG-TERM-MEMORY]\n"
            f"{state.get('long_term_memory') or '(empty)'}\n\n"
            "[THIS-TURN-ENRICHED-INPUT]\n"
            f"{state['working_text']}\n\n"
            "[THIS-TURN-REPLIES]\n"
            f"{delivered_reply_text or '(empty)'}"
        )
        messages = [
            SystemMessage(content=agent._system_prompt("memory")),
            HumanMessage(content=memory_input),
        ]

        try:
            msg = agent._invoke_model("memory", messages)
            parsed = agent._parse_memory_json(agent._message_to_text(msg.content))
            new_long_term = agent._normalize_memory_text(
                parsed.get("long_term_memory"),
                fallback=state.get("long_term_memory", ""),
            )
            if len(new_long_term) > 2000:
                compact_input = (
                    f"{MEMORY_JSON_INSTRUCTION}\n\n"
                    "你的记忆太长了，需要精简到2000字符以内。\n\n"
                    "[CURRENT-LONG-TERM-MEMORY]\n"
                    f"{new_long_term}"
                )
                compact_messages = [
                    SystemMessage(content=agent._system_prompt("memory")),
                    HumanMessage(content=compact_input),
                ]
                compact_msg = agent._invoke_model("memory", compact_messages)
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
            long_term_memory_updated = True
        except Exception as exc:
            memory_update_error = str(exc)
            agent._logger.warning(
                "Memory LLM update failed for conversation %s: %s",
                state["conversation_id"],
                exc,
            )
            new_long_term = str(state.get("long_term_memory") or "")

    tool_node_summary = str(state.get("tool_node_summary") or "").strip()
    intermediate_assistants = []
    if tool_node_summary:
        intermediate_assistants.append(
            messages_to_json([
                Message(
                    message_id=f"assistant-tool-{hash(tool_node_summary) & 0xFFFFFFFF}",
                    sender_id="assistant",
                    sender_name=agent._config.self_name or "assistant",
                    is_me=True,
                    segments=[MessageSegment(type="text", text=tool_node_summary)],
                )
            ])
        )

    output_events = state.get("output_events") or []
    is_all_silence = (
        not delivered_reply_text
        and all(str(getattr(evt, "content", "") or "").strip() == "bot选择沉默" for evt in output_events)
        and bool(output_events)
    )

    assistant_storage = ""
    if delivered_reply_text:
        assistant_storage = messages_to_json([
            Message(
                message_id=f"assistant-reply-{hash(delivered_reply_text) & 0xFFFFFFFF}",
                sender_id="assistant",
                sender_name=agent._config.self_name or "assistant",
                is_me=True,
                segments=[MessageSegment(type="text", text=delivered_reply_text)],
            )
        ])
    elif is_all_silence:
        silence_note = "[此消息记录本轮沉默，仅bot自身可见，其他群友未收到任何回复]\nbot选择沉默"
        assistant_storage = messages_to_json([
            Message(
                message_id="assistant-silence",
                sender_id="assistant",
                sender_name=agent._config.self_name or "assistant",
                is_me=True,
                segments=[MessageSegment(type="text", text=silence_note)],
            )
        ])

    agent._memory_store.persist_turn(
        conversation_id=state["conversation_id"],
        long_term_memory=new_long_term,
        user_message=str(
            state.get("user_messages_json", state.get("user_message", "")) or ""
        ),
        assistant_reply=assistant_storage,
        user_image_hashes=state.get("image_hashes") or [],
        intermediate_assistant_messages=intermediate_assistants,
    )
    openviking_user_message = str(state.get("user_message", "") or "")
    if str(agent._config.message_format or "yaml").strip().lower() == "yaml":
        openviking_user_message = str(
            state.get("user_messages_json", state.get("user_message", "")) or ""
        )
    openviking_user_message = agent._resolve_alias_descriptions(
        openviking_user_message, state
    )

    openviking_assistant_reply = agent._resolve_alias_descriptions(
        delivered_reply_text, state
    )

    openviking_commit_status = "disabled"
    openviking_tool_events = state.get("tool_events") or []
    try:
        commit_result = agent._commit_openviking_turn(
            conversation_id=state["conversation_id"],
            user_message=openviking_user_message,
            assistant_reply=openviking_assistant_reply,
            tool_events=list(openviking_tool_events),
        )
        openviking_commit_status = str(commit_result.get("status", "committed"))
    except Exception as exc:
        openviking_commit_status = "failed"
        agent._logger.warning(
            "OpenViking commit failed for conversation %s: %s",
            state["conversation_id"],
            exc,
        )

    if should_update_memory:
        attachment = (
            "[MEMORY-UPDATE]\n"
            f"long_term_memory_updated={'true' if long_term_memory_updated else 'false'}\n"
            f"long_term_memory_compacted={'true' if memory_compacted else 'false'}\n"
            "short_term_memory_appended=user+assistant\n"
            f"openviking_commit={openviking_commit_status}\n"
            f"openviking_tool_events={len(openviking_tool_events)}"
        )
        if memory_update_error:
            attachment += f"\nerror={memory_update_error}"
        agent._log_step_debug(
            state,
            "memory",
            attachment + f"\nupdated_long_term_memory:\n{new_long_term}",
        )
    else:
        appended_parts = ["user"]
        if intermediate_assistants:
            appended_parts.append("intermediate")
        attachment = (
            "[MEMORY-UPDATE]\n"
            "long_term_memory_updated=false\n"
            f"short_term_memory_appended={'+'.join(appended_parts)}\n"
            f"skipped_reason={state.get('memory_gate_result', 'skip')}"
        )
        agent._log_step_debug(state, "memory", attachment)

    return {
        "long_term_memory": new_long_term,
        "step_attachments": agent._set_attachment(state, "memory", attachment),
    }
