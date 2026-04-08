from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
from itertools import count
import json
from typing import Any, Literal, cast

import yaml
from kisaragirin import ConversationRequest, ImageInput

from .config_schema import MessageFormat


SegmentType = Literal[
    "text",
    "image",
    "reply",
    "at",
    "face",
    "record",
    "video",
    "file",
    "json",
    "forward",
    "poke",
    "dice",
    "rps",
]
SIMPLE_TIME_GAP_SECONDS = 3 * 60


@dataclass(slots=True)
class MessageSegmentData:
    type: SegmentType
    text: str = ""
    image: ImageInput | None = None
    image_name: str | None = None
    reply: MessageData | None = None
    reply_message_id: int | str | None = None
    at_user_id: int | None = None
    at_name: str = ""
    raw_data: dict[str, Any] | None = None
    forward_messages: list[MessageData] | None = None


@dataclass(slots=True)
class MessageData:
    message_id: int | str
    created_at: float
    sender_id: int
    sender_name: str
    mentioned_bot: bool
    segments: list[MessageSegmentData]
    has_unknown_segment: bool = False


def build_agent_request(
    *,
    conversation_id: str,
    platform: str,
    messages: list[MessageData],
    message_format: MessageFormat,
    debug: bool,
) -> ConversationRequest:
    images: list[ImageInput] = []
    image_index = count(1)
    image_hash_to_alias: dict[str, str] = {}
    payload_messages = [
        _serialize_message(
            message,
            image_index=image_index,
            images=images,
            image_hash_to_alias=image_hash_to_alias,
        )
        for message in messages
    ]

    payload = {
        "schema_version": 1,
        "platform": platform,
        "conversation": {
            "id": conversation_id,
            "type": "group",
        },
        "messages": payload_messages,
    }
    yaml_text = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
    request_message = yaml_text
    if message_format == "simple":
        request_message = _render_simple_payload(payload_messages)
    return ConversationRequest(
        conversation_id=conversation_id,
        message=request_message,
        storage_message=yaml_text,
        images=images,
        debug=debug,
    )


def _serialize_message(
    message: MessageData,
    *,
    image_index: count[int],
    images: list[ImageInput],
    image_hash_to_alias: dict[str, str],
) -> dict[str, object]:
    payload_segments: list[dict[str, object]] = []
    merged_blocks: list[str] = []

    for segment in message.segments:
        if segment.type == "text":
            item: dict[str, object] = {
                "type": "text",
                "text": segment.text,
            }
            _attach_raw_data(item, segment.raw_data)
            if payload_segments and payload_segments[-1].get("type") == "text":
                previous = payload_segments[-1].get("text", "")
                payload_segments[-1]["text"] = f"{previous}{segment.text}"
            else:
                payload_segments.append(item)
            merged_blocks.append(segment.text)
            continue

        if segment.type == "at":
            qq = str(segment.at_user_id) if segment.at_user_id is not None else ""
            name = str(segment.at_name or "").strip()
            text = segment.text or (f"@{name}" if name else "@(unknown)")
            item: dict[str, object] = {
                "type": "at",
                "text": text,
            }
            if qq:
                item["qq"] = qq
            if name:
                item["name"] = name
            _attach_raw_data(item, segment.raw_data)
            payload_segments.append(item)
            merged_blocks.append(text)
            continue

        if segment.type == "image" and segment.image is not None:
            alias = _get_or_create_image_alias(
                segment.image,
                image_index=image_index,
                images=images,
                image_hash_to_alias=image_hash_to_alias,
            )
            item: dict[str, object] = {
                "type": "image",
                "image": alias,
            }
            if segment.image_name:
                item["name"] = segment.image_name
            _attach_raw_data(item, segment.raw_data)
            payload_segments.append(item)
            merged_blocks.append(alias)
            continue

        if segment.type == "reply":
            reply_message_id = (
                str(segment.reply_message_id) if segment.reply_message_id is not None else ""
            )
            if segment.reply is None:
                payload_segments.append(
                    {
                        "type": "reply",
                        "reply_to_message_id": reply_message_id or "(unknown)",
                        "reply_to_message": "(unavailable)",
                    }
                )
            else:
                nested = _serialize_message(
                    segment.reply,
                    image_index=image_index,
                    images=images,
                    image_hash_to_alias=image_hash_to_alias,
                )
                payload_segments.append(
                    {
                        "type": "reply",
                        "reply_to_message_id": reply_message_id or str(segment.reply.message_id),
                        "reply_to_message": nested,
                    }
                )
            _attach_raw_data(payload_segments[-1], segment.raw_data)
            merged_blocks.append(
                f"[reply:{reply_message_id or (str(segment.reply.message_id) if segment.reply else 'unknown')}]"
            )
            continue

        item = _serialize_misc_segment(
            segment,
            image_index=image_index,
            images=images,
            image_hash_to_alias=image_hash_to_alias,
        )
        if item is None:
            continue
        payload_segments.append(item)
        placeholder = _render_simple_inline_segment(item)
        if placeholder:
            merged_blocks.append(placeholder)

    timestamp = datetime.fromtimestamp(message.created_at).astimezone()
    payload: dict[str, object] = {
        "message_id": str(message.message_id),
        "sent_at_local": timestamp.isoformat(),
        "sender": {
            "id": message.sender_id,
            "name": message.sender_name,
        },
        "mentioned_bot": message.mentioned_bot,
        "segments": payload_segments,
    }
    if message.has_unknown_segment:
        payload["merged_text"] = "".join(merged_blocks)
    return payload


def _attach_raw_data(item: dict[str, object], raw_data: dict[str, Any] | None) -> None:
    if raw_data:
        item["data"] = raw_data


def _serialize_misc_segment(
    segment: MessageSegmentData,
    *,
    image_index: count[int],
    images: list[ImageInput],
    image_hash_to_alias: dict[str, str],
) -> dict[str, object] | None:
    item: dict[str, object] = {"type": segment.type}
    _attach_raw_data(item, segment.raw_data)

    if segment.type == "forward":
        forward_id = ""
        if segment.raw_data:
            forward_id = str(segment.raw_data.get("id") or "").strip()
        if forward_id:
            item["forward_id"] = forward_id
        if segment.forward_messages:
            item["forward_messages"] = [
                _serialize_message(
                    forward_message,
                    image_index=image_index,
                    images=images,
                    image_hash_to_alias=image_hash_to_alias,
                )
                for forward_message in segment.forward_messages
            ]
        return item

    if segment.type == "face":
        face_name = str(segment.text or "").strip()
        if face_name:
            item["name"] = face_name
        return item

    if segment.type in {"record", "video", "file", "json", "poke", "dice", "rps"}:
        return item

    return None


def _image_sha256(image: ImageInput) -> str:
    raw = str(getattr(image, "base64_data", "") or "").strip()
    if not raw:
        return ""
    try:
        decoded = base64.b64decode(raw, validate=False)
    except Exception:
        return ""
    return hashlib.sha256(decoded).hexdigest()


def _get_or_create_image_alias(
    image: ImageInput,
    *,
    image_index: count[int],
    images: list[ImageInput],
    image_hash_to_alias: dict[str, str],
) -> str:
    image_hash = _image_sha256(image)
    if image_hash:
        existing = image_hash_to_alias.get(image_hash)
        if existing is not None:
            return existing
    alias = f"[image-{next(image_index)}]"
    images.append(image)
    if image_hash:
        image_hash_to_alias[image_hash] = alias
    return alias


def _render_simple_payload(messages: list[dict[str, object]]) -> str:
    blocks: list[str] = []
    block_started_at: datetime | None = None
    for message in messages:
        timestamp = _parse_sent_at_local(message)
        if timestamp is not None and (
            block_started_at is None
            or (timestamp - block_started_at).total_seconds() > SIMPLE_TIME_GAP_SECONDS
            ):
            blocks.append(timestamp.strftime("%Y-%m-%d %H:%M"))
            block_started_at = timestamp
        blocks.append(_render_simple_message(message))
    if not blocks:
        return "---\n---"
    return "---\n" + "\n---\n".join(blocks) + "\n---"


def _parse_sent_at_local(message: dict[str, object]) -> datetime | None:
    raw = str(message.get("sent_at_local", "") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _render_simple_message(message: dict[str, object]) -> str:
    sender_name = _message_sender_name(message)
    content, reference_lines = _render_message_content(message, reply_depth=1)
    prefix = "(有人@我)" if bool(message.get("mentioned_bot")) else ""
    header = f"{prefix}[{sender_name}]:"
    if content:
        header = f"{header} {content}"
    if not reference_lines:
        return header
    lines = [header]
    lines.extend(f"  {line}" for line in reference_lines)
    return "\n".join(lines)


def _render_message_content(
    message: dict[str, object],
    *,
    reply_depth: int,
) -> tuple[str, list[str]]:
    inline_parts: list[str] = []
    reference_lines: list[str] = []
    segments = message.get("segments")
    if not isinstance(segments, list):
        segments = []
    for raw_segment in segments:
        if not isinstance(raw_segment, dict):
            continue
        segment = cast(dict[str, object], raw_segment)
        segment_type = str(segment.get("type", "") or "").strip()
        if segment_type == "text":
            _append_inline_part(inline_parts, str(segment.get("text", "") or ""))
            continue
        if segment_type == "at":
            _append_inline_part(inline_parts, str(segment.get("text", "") or ""))
            continue
        if segment_type == "image":
            _append_inline_part(inline_parts, str(segment.get("image", "") or ""))
            continue
        if segment_type == "reply":
            if reply_depth <= 0:
                reply_id = str(segment.get("reply_to_message_id", "") or "").strip()
                _append_inline_part(
                    inline_parts,
                    f"[reply:{reply_id or 'unknown'}]",
                )
                continue

            reference_lines.append(_render_reference_line(segment, reply_depth=reply_depth))
            continue

        if segment_type == "forward":
            forward_lines = _render_forward_reference_lines(segment, reply_depth=reply_depth)
            if forward_lines:
                reference_lines.extend(forward_lines)
                continue
            forward_id = str(segment.get("forward_id", "") or "").strip()
            _append_inline_part(
                inline_parts,
                f"[forward:{forward_id or 'unknown'}]",
            )
            continue

        placeholder = _render_simple_inline_segment(segment)
        if placeholder:
            _append_inline_part(inline_parts, placeholder)

    inline_text = "".join(inline_parts).strip()
    if not inline_text:
        inline_text = str(message.get("merged_text", "") or "").strip()
    return inline_text, reference_lines


def _render_reference_line(
    reply_segment: dict[str, object],
    *,
    reply_depth: int,
) -> str:
    nested = reply_segment.get("reply_to_message")
    reply_id = str(reply_segment.get("reply_to_message_id", "") or "").strip()
    if not isinstance(nested, dict):
        return f"[ref {reply_id or 'unknown'}]：(unavailable)"
    nested_message = cast(dict[str, object], nested)

    sender_name = _message_sender_name(nested_message)
    content, _ = _render_message_content(nested_message, reply_depth=reply_depth - 1)
    if not content:
        content = "(empty)"
    return f"[ref {sender_name}]：{content}"


def _render_forward_reference_lines(
    forward_segment: dict[str, object],
    *,
    reply_depth: int,
) -> list[str]:
    raw_messages = forward_segment.get("forward_messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        return []

    lines: list[str] = []
    for raw_message in raw_messages:
        if not isinstance(raw_message, dict):
            continue
        message = cast(dict[str, object], raw_message)
        sender_name = _message_sender_name(message)
        content, _ = _render_message_content(message, reply_depth=reply_depth - 1)
        if not content:
            content = "(empty)"
        lines.append(f"[forward {sender_name}]：{content}")
    return lines


def _message_sender_name(message: dict[str, object]) -> str:
    sender = message.get("sender")
    if not isinstance(sender, dict):
        return "unknown"
    sender_data = cast(dict[str, object], sender)
    name = str(sender_data.get("name", "") or "").strip()
    if bool(sender_data.get("is_me")) and name:
        return f"{name}(me)"
    if name:
        return name
    sender_id = str(sender_data.get("id", "") or "").strip()
    return sender_id or "unknown"


def _append_inline_part(parts: list[str], value: str) -> None:
    text = str(value or "")
    if not text:
        return
    if not parts:
        parts.append(text)
        return
    previous = parts[-1]
    if previous.endswith((" ", "\n")) or text.startswith(
        (" ", "\n", "，", "。", "！", "？", "、", ",", ".", "!", "?")
    ):
        parts.append(text)
        return
    parts.append(f" {text}")


def _render_simple_inline_segment(segment: dict[str, object]) -> str:
    segment_type = str(segment.get("type", "") or "").strip()
    data = segment.get("data")
    raw = cast(dict[str, object], data) if isinstance(data, dict) else {}

    if segment_type == "face":
        name = str(segment.get("name", "") or "").strip()
        if not name:
            name = str(raw.get("id", "") or "").strip() or "unknown"
        return f"[face: {name}]"

    if segment_type == "record":
        return "[record: 语音]"

    if segment_type in {"video", "file"}:
        name = _segment_file_name(raw)
        label = name or "unknown"
        return f"[{segment_type}: {label}]"

    if segment_type == "json":
        return f"[json: {_json_segment_text(raw)}]"

    if segment_type == "poke":
        return f"[poke: {_joined_segment_detail(raw, keys=('type', 'id')) or 'unknown'}]"

    if segment_type in {"dice", "rps"}:
        result = str(raw.get("result", "") or "").strip() or "unknown"
        return f"[{segment_type}: {result}]"

    return ""


def _segment_file_name(raw_data: dict[str, object]) -> str:
    for key in ("name", "file", "path", "file_id"):
        value = str(raw_data.get(key, "") or "").strip()
        if value:
            return value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return ""


def _json_segment_text(raw_data: dict[str, object]) -> str:
    value = raw_data.get("data", "")
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value or "")


def _joined_segment_detail(raw_data: dict[str, object], *, keys: tuple[str, ...]) -> str:
    parts = [str(raw_data.get(key, "") or "").strip() for key in keys]
    normalized = [part for part in parts if part]
    return "/".join(normalized)
