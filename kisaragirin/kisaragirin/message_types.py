from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal, cast


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
class MessageSegment:
    type: SegmentType
    text: str = ""
    image: str = ""
    name: str = ""
    qq: str = ""
    reply_to_message_id: str = ""
    reply_to_message: Message | None = None
    forward_id: str = ""
    forward_messages: list[Message] = field(default_factory=list)
    data: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class Message:
    message_id: str
    sent_at_local: str = ""
    sender_id: str = ""
    sender_name: str = ""
    is_me: bool = False
    mentioned_bot: bool = False
    segments: list[MessageSegment] = field(default_factory=list)
    merged_text: str = ""


def segment_to_dict(seg: MessageSegment) -> dict[str, object]:
    result: dict[str, object] = {"type": seg.type}
    if seg.text:
        result["text"] = seg.text
    if seg.image:
        result["image"] = seg.image
    if seg.name:
        result["name"] = seg.name
    if seg.qq:
        result["qq"] = seg.qq
    if seg.reply_to_message_id:
        result["reply_to_message_id"] = seg.reply_to_message_id
    if seg.reply_to_message is not None:
        result["reply_to_message"] = message_to_dict(seg.reply_to_message)
    if seg.forward_id:
        result["forward_id"] = seg.forward_id
    if seg.forward_messages:
        result["forward_messages"] = [message_to_dict(m) for m in seg.forward_messages]
    if seg.data:
        result["data"] = seg.data
    return result


def message_to_dict(msg: Message) -> dict[str, object]:
    result: dict[str, object] = {
        "message_id": msg.message_id,
        "sender_id": msg.sender_id,
        "sender_name": msg.sender_name,
        "is_me": msg.is_me,
        "mentioned_bot": msg.mentioned_bot,
        "segments": [segment_to_dict(s) for s in msg.segments],
    }
    if msg.sent_at_local:
        result["sent_at_local"] = msg.sent_at_local
    if msg.merged_text:
        result["merged_text"] = msg.merged_text
    return result


def dict_to_segment(d: dict[str, object]) -> MessageSegment:
    return MessageSegment(
        type=str(d.get("type", "") or "").strip(),  # type: ignore[arg-type]
        text=str(d.get("text", "") or ""),
        image=str(d.get("image", "") or ""),
        name=str(d.get("name", "") or ""),
        qq=str(d.get("qq", "") or ""),
        reply_to_message_id=str(d.get("reply_to_message_id", "") or ""),
        reply_to_message=dict_to_message(cast(dict[str, object], d["reply_to_message"])) if isinstance(d.get("reply_to_message"), dict) else None,
        forward_id=str(d.get("forward_id", "") or ""),
        forward_messages=[dict_to_message(cast(dict[str, object], m)) for m in cast(list[object], d.get("forward_messages", [])) if isinstance(m, dict)],
        data=dict(cast(dict[str, object], d.get("data", {}))) if isinstance(d.get("data"), dict) else {},
    )


def dict_to_message(d: dict[str, object]) -> Message:
    raw_segments = d.get("segments")
    segments: list[MessageSegment] = []
    if isinstance(raw_segments, list):
        for raw_seg in raw_segments:
            if isinstance(raw_seg, dict):
                segments.append(dict_to_segment(cast(dict[str, object], raw_seg)))

    sender_id = str(d.get("sender_id", "") or "")
    sender_name = str(d.get("sender_name", "") or "")
    is_me = bool(d.get("is_me", False))

    raw_sender = d.get("sender")
    if isinstance(raw_sender, dict):
        sender_data = cast(dict[str, object], raw_sender)
        if not sender_id:
            sender_id = str(sender_data.get("id", "") or "")
        if not sender_name:
            sender_name = str(sender_data.get("name", "") or "")
        if not is_me:
            is_me = bool(sender_data.get("is_me", False))

    return Message(
        message_id=str(d.get("message_id", "") or ""),
        sent_at_local=str(d.get("sent_at_local", "") or ""),
        sender_id=sender_id,
        sender_name=sender_name,
        is_me=is_me,
        mentioned_bot=bool(d.get("mentioned_bot", False)),
        segments=segments,
        merged_text=str(d.get("merged_text", "") or ""),
    )


def messages_to_json(messages: list[Message]) -> str:
    return json.dumps([message_to_dict(m) for m in messages], ensure_ascii=False, separators=(",", ":"))


def messages_from_json(text: str) -> list[Message] | None:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(loaded, list):
        return None
    return [dict_to_message(m) for m in loaded if isinstance(m, dict)]
