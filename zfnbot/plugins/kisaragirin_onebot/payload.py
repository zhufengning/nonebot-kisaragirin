from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import count
from typing import Literal

import yaml
from kisaragirin import ConversationRequest, ImageInput


SegmentType = Literal["text", "image", "reply"]


@dataclass(slots=True)
class MessageSegmentData:
    type: SegmentType
    text: str = ""
    image: ImageInput | None = None
    image_name: str | None = None
    reply: MessageData | None = None
    reply_message_id: int | str | None = None


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
    payload_messages = [
        _serialize_message(message, image_index=image_index, images=images) for message in messages
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
) -> dict[str, object]:
    payload_segments: list[dict[str, object]] = []
    merged_blocks: list[str] = []

    for segment in message.segments:
        if segment.type == "text":
            payload_segments.append(
                {
                    "type": "text",
                    "text": segment.text,
                }
            )
            merged_blocks.append(segment.text)
            continue

        if segment.type == "image" and segment.image is not None:
            alias = f"[image-{next(image_index)}]"
            item: dict[str, object] = {
                "type": "image",
                "image": alias,
            }
            if segment.image_name:
                item["name"] = segment.image_name
            payload_segments.append(item)
            merged_blocks.append(alias)
            images.append(segment.image)
            continue

        if segment.type == "reply":
            reply_message_id = (
                str(segment.reply_message_id) if segment.reply_message_id is not None else ""
            )
            if segment.reply is None:
                payload_segments.append(
                    {
                        "type": "reply",
                        "message_id": reply_message_id or "(unknown)",
                        "message": "(unavailable)",
                    }
                )
            else:
                nested = _serialize_message(segment.reply, image_index=image_index, images=images)
                payload_segments.append(
                    {
                        "type": "reply",
                        "message_id": reply_message_id or str(segment.reply.message_id),
                        "message": nested,
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
