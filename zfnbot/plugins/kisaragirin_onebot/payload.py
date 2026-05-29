from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from typing import Any, Literal

from kisaragirin import ConversationRequest, ImageInput, Message, MessageSegment


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
    messages: list[MessageData],
    bot_id: str = "",
    debug: bool,
) -> ConversationRequest:
    images: list[ImageInput] = []
    image_index = count(1)
    image_hash_to_alias: dict[str, str] = {}
    k_messages = [
        _convert_message(
            message,
            bot_id=bot_id,
            image_index=image_index,
            images=images,
            image_hash_to_alias=image_hash_to_alias,
        )
        for message in messages
    ]
    return ConversationRequest(
        conversation_id=conversation_id,
        messages=k_messages,
        images=images,
        debug=debug,
    )


def _convert_message(
    message: MessageData,
    *,
    bot_id: str = "",
    image_index: count[int],
    images: list[ImageInput],
    image_hash_to_alias: dict[str, str],
) -> Message:
    segments: list[MessageSegment] = []
    merged_blocks: list[str] = []

    for segment in message.segments:
        if segment.type == "text":
            if segments and segments[-1].type == "text":
                segments[-1].text = f"{segments[-1].text}{segment.text}"
            else:
                segments.append(MessageSegment(type="text", text=segment.text))
            merged_blocks.append(segment.text)
            continue

        if segment.type == "at":
            name = str(segment.at_name or "").strip()
            text = segment.text or (f"@{name}" if name else "@(unknown)")
            seg = MessageSegment(type="at", text=text)
            if segment.at_user_id is not None:
                seg.qq = str(segment.at_user_id)
            if name:
                seg.name = name
            _attach_raw_data(seg, segment.raw_data)
            segments.append(seg)
            merged_blocks.append(text)
            continue

        if segment.type == "image" and segment.image is not None:
            alias = _get_or_create_image_alias(
                segment.image,
                image_index=image_index,
                images=images,
                image_hash_to_alias=image_hash_to_alias,
            )
            seg = MessageSegment(type="image", image=alias)
            if segment.image_name:
                seg.name = segment.image_name
            _attach_raw_data(seg, segment.raw_data)
            segments.append(seg)
            merged_blocks.append(alias)
            continue

        if segment.type == "reply":
            reply_message_id = (
                str(segment.reply_message_id) if segment.reply_message_id is not None else ""
            )
            if segment.reply is None:
                seg = MessageSegment(
                    type="reply",
                    reply_to_message_id=reply_message_id or "(unknown)",
                )
                _attach_raw_data(seg, segment.raw_data)
                segments.append(seg)
            else:
                nested = _convert_message(
                    segment.reply,
                    bot_id=bot_id,
                    image_index=image_index,
                    images=images,
                    image_hash_to_alias=image_hash_to_alias,
                )
                seg = MessageSegment(
                    type="reply",
                    reply_to_message_id=reply_message_id or str(segment.reply.message_id),
                    reply_to_message=nested,
                )
                _attach_raw_data(seg, segment.raw_data)
                segments.append(seg)
            merged_blocks.append(
                f"[reply:{reply_message_id or (str(segment.reply.message_id) if segment.reply else 'unknown')}]"
            )
            continue

        seg = _convert_misc_segment(
            segment,
            bot_id=bot_id,
            image_index=image_index,
            images=images,
            image_hash_to_alias=image_hash_to_alias,
        )
        if seg is None:
            continue
        segments.append(seg)
        placeholder = _inline_placeholder(seg)
        if placeholder:
            merged_blocks.append(placeholder)

    timestamp = datetime.fromtimestamp(message.created_at).astimezone()
    return Message(
        message_id=str(message.message_id),
        sent_at_local=timestamp.isoformat(),
        sender_id=str(message.sender_id),
        sender_name=message.sender_name,
        is_me=str(message.sender_id) == str(bot_id),
        mentioned_bot=message.mentioned_bot,
        segments=segments,
        merged_text="".join(merged_blocks) if message.has_unknown_segment else "",
    )


def _attach_raw_data(seg: MessageSegment, raw_data: dict[str, Any] | None) -> None:
    if raw_data:
        seg.data = raw_data


def _convert_misc_segment(
    segment: MessageSegmentData,
    *,
    bot_id: str = "",
    image_index: count[int],
    images: list[ImageInput],
    image_hash_to_alias: dict[str, str],
) -> MessageSegment | None:
    seg = MessageSegment(type=segment.type)
    _attach_raw_data(seg, segment.raw_data)

    if segment.type == "forward":
        forward_id = ""
        if segment.raw_data:
            forward_id = str(segment.raw_data.get("id") or "").strip()
        if forward_id:
            seg.forward_id = forward_id
        if segment.forward_messages:
            seg.forward_messages = [
                _convert_message(
                    forward_message,
                    bot_id=bot_id,
                    image_index=image_index,
                    images=images,
                    image_hash_to_alias=image_hash_to_alias,
                )
                for forward_message in segment.forward_messages
            ]
        return seg

    if segment.type == "face":
        face_name = str(segment.text or "").strip()
        if face_name:
            seg.name = face_name
        return seg

    if segment.type in {"record", "video", "file", "json", "poke", "dice", "rps"}:
        return seg

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


def _inline_placeholder(seg: MessageSegment) -> str:
    if seg.type == "face":
        name = seg.name.strip() or seg.data.get("id", "") or "unknown"
        return f"[face: {name}]"
    if seg.type == "record":
        return "[record: 语音]"
    if seg.type in {"video", "file"}:
        name = _segment_file_name(seg.data) or "unknown"
        return f"[{seg.type}: {name}]"
    if seg.type == "json":
        return f"[json: {_json_segment_text(seg.data)}]"
    if seg.type == "poke":
        detail = _joined_segment_detail(seg.data, keys=("type", "id")) or "unknown"
        return f"[poke: {detail}]"
    if seg.type in {"dice", "rps"}:
        result = str(seg.data.get("result", "") or "").strip() or "unknown"
        return f"[{seg.type}: {result}]"
    return ""


def _segment_file_name(raw_data: dict[str, object]) -> str:
    for key in ("name", "file", "path", "file_id"):
        value = str(raw_data.get(key, "") or "").strip()
        if value:
            return value.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    return ""


def _json_segment_text(raw_data: dict[str, object]) -> str:
    import json as _json

    value = raw_data.get("data", "")
    if isinstance(value, str):
        return value
    try:
        return _json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:
        return str(value or "")


def _joined_segment_detail(raw_data: dict[str, object], *, keys: tuple[str, ...]) -> str:
    parts = [str(raw_data.get(key, "") or "").strip() for key in keys]
    normalized = [part for part in parts if part]
    return "/".join(normalized)
