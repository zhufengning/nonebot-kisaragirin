from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from typing import Literal

import yaml
from kisaragirin import ConversationRequest, ImageInput


SegmentType = Literal["text", "image"]


@dataclass(slots=True)
class MessageSegmentData:
    type: SegmentType
    text: str = ""
    image: ImageInput | None = None
    image_name: str | None = None


@dataclass(slots=True)
class MessageData:
    message_id: int | str
    created_at: float
    sender_id: int
    sender_name: str
    mentioned_bot: bool
    segments: list[MessageSegmentData]


def build_agent_request(
    *,
    conversation_id: str,
    platform: str,
    messages: list[MessageData],
    debug: bool,
) -> ConversationRequest:
    images: list[ImageInput] = []
    image_index = count(1)
    payload_messages: list[dict[str, object]] = []

    for message in messages:
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

        timestamp = datetime.fromtimestamp(message.created_at, tz=timezone.utc)
        payload_messages.append(
            {
                "message_id": str(message.message_id),
                "sent_at": {
                    "unix": message.created_at,
                    "iso_utc": timestamp.isoformat(),
                },
                "sender": {
                    "id": message.sender_id,
                    "name": message.sender_name,
                },
                "mentioned_bot": message.mentioned_bot,
                "merged_text": "".join(merged_blocks),
                "segments": payload_segments,
            }
        )

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
