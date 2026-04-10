from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from .config import ImageInput


def run_urls(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    url_aliases = state.get("url_aliases") or {}
    if not url_aliases:
        appendix = "[URL-SUMMARIES]\n(no url detected)"
        agent._log_step_debug(state, "url", appendix)
        return {
            "url_appendix": appendix,
            "step_attachments": agent._set_attachment(state, "url", appendix),
        }

    blocks: list[str] = ["[URL-SUMMARIES]"]
    summary_by_url: dict[str, str] = {}

    for idx, (alias, url) in enumerate(url_aliases.items(), start=1):
        summary, _, _ = agent._get_or_create_url_summary(
            alias=alias,
            url=url,
            summary_by_url=summary_by_url,
        )
        blocks.append(
            f"{idx}. {alias}\n"
            f"[SUMMARY]\n{summary}"
        )

    appendix = "\n\n".join(blocks)
    agent._log_step_debug(state, "url", appendix)
    return {
        "url_appendix": appendix,
        "step_attachments": agent._set_attachment(state, "url", appendix),
    }


def run_vision(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    images = state.get("images") or []
    all_image_hashes = state.get("all_image_hashes") or []
    image_hash_to_alias = state.get("image_hash_to_alias") or {}
    if not images and not all_image_hashes:
        appendix = "[IMAGE-DESCRIPTIONS]\n(no image input)"
        agent._log_step_debug(state, "vision", appendix)
        return {
            "vision_appendix": appendix,
            "step_attachments": agent._set_attachment(state, "vision", appendix),
        }

    image_aliases = state.get("image_aliases") or []
    image_hashes = state.get("image_hashes") or []
    description_by_hash: dict[str, str] = {}
    hashless_items: list[tuple[str, ImageInput]] = []
    for idx, image in enumerate(images, start=1):
        image_hash = image_hashes[idx - 1] if idx - 1 < len(image_hashes) else ""
        alias = (
            image_aliases[idx - 1]
            if idx - 1 < len(image_aliases)
            else f"[image-{idx}]"
        )
        normalized_hash = str(image_hash).strip().lower()
        if not normalized_hash:
            hashless_items.append((alias, image))
            continue
        description = agent._get_or_create_image_description(
            image=image,
            image_hash=normalized_hash,
            description_by_hash=description_by_hash,
        )
        description_by_hash[normalized_hash] = description

    blocks: list[str] = ["[IMAGE-DESCRIPTIONS]"]
    item_index = 1
    for image_hash in all_image_hashes:
        normalized_hash = str(image_hash).strip().lower()
        if not normalized_hash:
            continue
        alias = image_hash_to_alias.get(normalized_hash)
        if not alias:
            continue
        description = description_by_hash.get(normalized_hash)
        if description is None:
            description = agent._memory_store.get_image_description(normalized_hash)
        if description is None:
            description = "(description cache miss)"
        blocks.append(f"{item_index}. {alias}\n{description}")
        item_index += 1

    for alias, image in hashless_items:
        description = agent._describe_image(image)
        blocks.append(f"{item_index}. {alias}\n{description}")
        item_index += 1

    appendix = "\n\n".join(blocks)
    agent._log_step_debug(state, "vision", appendix)
    return {
        "vision_appendix": appendix,
        "step_attachments": agent._set_attachment(state, "vision", appendix),
    }


def run_enrich_merge(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    base = str(state.get("working_text_base", state.get("working_text", "")))
    url_appendix = str(state.get("url_appendix", "") or "").strip()
    vision_appendix = str(state.get("vision_appendix", "") or "").strip()
    parts = [base]
    if url_appendix:
        parts.append(url_appendix)
    if vision_appendix:
        parts.append(vision_appendix)
    working_text = "\n\n".join(part for part in parts if part)
    return {
        "working_text": working_text,
    }


def run_tools(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    tool_model = agent._model(agent._config.step_models.tool).bind_tools(agent._tools)
    tool_input = agent._tool_scoped_working_text(state)
    messages: list[Any] = [
        SystemMessage(content=agent._system_prompt("tool")),
        HumanMessage(content=tool_input),
    ]

    logs: list[str] = ["[TOOL-EXTRA-INFO]"]
    used_tool = False

    for round_idx in range(1, agent._config.max_tool_rounds + 1):
        raw_ai_message = tool_model.invoke(messages)
        ai_message = raw_ai_message
        if isinstance(raw_ai_message, AIMessage):
            ai_message = AIMessage(
                content=agent._message_to_text(raw_ai_message.content),
                tool_calls=raw_ai_message.tool_calls,
            )
        messages.append(ai_message)

        tool_calls = ai_message.tool_calls if isinstance(ai_message, AIMessage) else []
        if not tool_calls:
            final_note = agent._message_to_text(ai_message.content)
            if final_note.strip():
                logs.append(f"[ROUND-{round_idx}-MODEL-NOTE]\n{final_note.strip()}")
            break

        used_tool = True
        for call_idx, tool_call in enumerate(tool_calls, start=1):
            tool_name = tool_call.get("name", "")
            tool_args = tool_call.get("args", {})
            tool_id = tool_call.get("id", f"round-{round_idx}-call-{call_idx}")

            tool_output = agent._invoke_tool(tool_name, tool_args)
            if len(tool_output) > agent._config.max_tool_output_chars:
                tool_output = (
                    tool_output[: agent._config.max_tool_output_chars]
                    + "\n...<truncated>"
                )

            logs.append(
                f"[ROUND-{round_idx}-TOOL-{call_idx}] {tool_name}\n"
                f"args={json.dumps(tool_args, ensure_ascii=False)}\n"
                f"output:\n{tool_output}"
            )

            messages.append(
                ToolMessage(content=tool_output, tool_call_id=tool_id, name=tool_name)
            )

    if not used_tool:
        logs.append("No tool was called.")

    appendix = "\n\n".join(logs)
    return {
        "working_text": state["working_text"] + "\n\n" + appendix,
    }
