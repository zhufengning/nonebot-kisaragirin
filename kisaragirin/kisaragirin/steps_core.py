from __future__ import annotations

from typing import Any


def run_step0_prepare(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
    conversation_id = state["conversation_id"]
    normalized_message, url_aliases = agent._replace_urls_with_aliases(
        state["user_message"]
    )
    image_aliases = [
        f"[image-{idx}]" for idx, _ in enumerate(state.get("images") or [], start=1)
    ]
    image_hashes = [
        agent._compute_image_sha256(image) for image in (state.get("images") or [])
    ]
    long_term_memory_raw = agent._memory_store.get_long_term(conversation_id)
    long_term_memory = agent._replace_legacy_image_hash_aliases(long_term_memory_raw)
    short_term_messages = agent._memory_store.get_short_term(
        conversation_id=conversation_id,
        turn_window=agent._config.short_term_turn_window,
    )
    short_term_image_hashes = agent._memory_store.get_short_term_image_hashes(
        conversation_id=conversation_id,
        turn_window=agent._config.short_term_turn_window,
    )
    short_term_image_refs = agent._memory_store.get_short_term_image_refs(
        conversation_id=conversation_id,
        turn_window=agent._config.short_term_turn_window,
    )
    short_term_urls = agent._extract_short_term_urls(short_term_messages)
    url_to_alias = {url: alias for alias, url in url_aliases.items()}
    next_url_index = len(url_aliases) + 1
    for url in short_term_urls:
        if url in url_to_alias:
            continue
        alias = f"[url-{next_url_index}]"
        next_url_index += 1
        url_to_alias[url] = alias
        url_aliases[alias] = url

    image_hash_to_alias: dict[str, str] = {}
    all_image_hashes: list[str] = []
    for idx, image_hash in enumerate(image_hashes, start=1):
        normalized_hash = str(image_hash).strip().lower()
        if not normalized_hash or normalized_hash in image_hash_to_alias:
            continue
        alias = f"[image-{idx}]"
        image_hash_to_alias[normalized_hash] = alias
        all_image_hashes.append(normalized_hash)

    all_image_aliases = list(image_aliases)
    next_image_index = len(image_aliases) + 1
    for image_hash in short_term_image_hashes:
        normalized_hash = str(image_hash).strip().lower()
        if not normalized_hash or normalized_hash in image_hash_to_alias:
            continue
        alias = f"[image-{next_image_index}]"
        next_image_index += 1
        image_hash_to_alias[normalized_hash] = alias
        all_image_hashes.append(normalized_hash)
        all_image_aliases.append(alias)

    short_term_context = agent._format_short_term_context(
        short_term_messages,
        short_term_image_refs=short_term_image_refs,
        short_term_hash_to_alias=image_hash_to_alias,
        short_term_url_to_alias=url_to_alias,
    )
    url_alias_text = ", ".join(url_aliases.keys()) or "(none)"
    image_alias_text = agent._format_image_alias_text(all_image_aliases)

    working_text = (
        "[STEP-0-LONG-TERM-MEMORY]\n"
        f"{long_term_memory or '(empty)'}\n\n"
        "[STEP-0-FIXED-MEMORY]\n"
        f"{agent._config.prompts.fixed_memory or '(empty)'}\n\n"
        "[STEP-0-SHORT-TERM-CONTEXT]\n"
        f"{short_term_context}\n\n"
        "[STEP-0-RESOURCE-ALIASES]\n"
        f"urls: {url_alias_text}\n"
        f"images: {image_alias_text}\n\n"
        "[STEP-0-ORIGINAL-INPUT]\n"
        f"{normalized_message}"
    )
    attachment_text = (
        "[STEP-0-LONG-TERM-MEMORY]\n"
        f"{long_term_memory or '(empty)'}\n\n"
        "[STEP-0-SHORT-TERM-CONTEXT]\n"
        f"{short_term_context}\n\n"
        "[STEP-0-RESOURCE-ALIASES]\n"
        f"urls: {url_alias_text}\n"
        f"images: {image_alias_text}\n"
    )
    agent._log_step_debug(state, "STEP-0", attachment_text)

    return {
        "user_message_normalized": normalized_message,
        "url_aliases": url_aliases,
        "url_to_alias": url_to_alias,
        "image_aliases": image_aliases,
        "image_hashes": image_hashes,
        "all_image_aliases": all_image_aliases,
        "all_image_hashes": all_image_hashes,
        "image_hash_to_alias": image_hash_to_alias,
        "long_term_memory": long_term_memory,
        "short_term_context": short_term_context,
        "working_text": working_text,
        "working_text_base": working_text,
        "step_attachments": agent._set_attachment(
            state,
            "STEP-0",
            attachment_text,
        ),
    }
