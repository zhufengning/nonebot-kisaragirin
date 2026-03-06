from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime
from itertools import count
from typing import Literal

import yaml
from kisaragirin import ConversationRequest, ImageInput


SegmentType = Literal["text", "image", "reply", "at"]


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
    return ConversationRequest(
        conversation_id=conversation_id,
        message=yaml_text,
        images=images,
        debug=debug,
    )


def _serialize_message(
    message: MessageData,
    *,
    image_index: count,
    images: list[ImageInput],
    image_hash_to_alias: dict[str, str],
) -> dict[str, object]:
    payload_segments: list[dict[str, object]] = []
    merged_blocks: list[str] = []

    for segment in message.segments:
        if segment.type == "text":
            if payload_segments and payload_segments[-1].get("type") == "text":
                previous = payload_segments[-1].get("text", "")
                payload_segments[-1]["text"] = f"{previous}{segment.text}"
            else:
                payload_segments.append(
                    {
                        "type": "text",
                        "text": segment.text,
                    }
                )
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
            merged_blocks.append(
                f"[reply:{reply_message_id or (str(segment.reply.message_id) if segment.reply else 'unknown')}]"
            )

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
    image_index: count,
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
