from __future__ import annotations

from typing import Any


def run_prepare(agent: Any, state: dict[str, Any]) -> dict[str, Any]:
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
    openviking_memory = agent._search_openviking_memories(
        conversation_id=conversation_id,
        query=normalized_message,
    )
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
        alias = agent._format_url_alias(next_url_index, url)
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
        message_format=agent._config.message_format,
        self_name=agent._config.self_name,
        short_term_image_refs=short_term_image_refs,
        short_term_hash_to_alias=image_hash_to_alias,
        short_term_url_to_alias=url_to_alias,
    )
    working_text = (
        "[LONG-TERM-MEMORY]\n"
        f"{long_term_memory or '(empty)'}\n\n"
        + (
            f"{openviking_memory.strip()}\n\n"
            if str(openviking_memory or "").strip()
            else ""
        )
        + "[FIXED-MEMORY]\n"
        f"{agent._config.prompts.fixed_memory or '(empty)'}\n\n"
        "[SHORT-TERM-CONTEXT]\n"
        f"{short_term_context}\n\n"
        "[ORIGINAL-INPUT]\n"
        f"{normalized_message}"
    )
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
        "openviking_memory": openviking_memory,
        "short_term_context": short_term_context,
        "working_text": working_text,
        "working_text_base": working_text,
    }
