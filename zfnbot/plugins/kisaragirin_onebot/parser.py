from __future__ import annotations

import base64
import csv
from io import BytesIO
import mimetypes
import re
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote_to_bytes

import httpx
from kisaragirin import ImageInput
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment
from PIL import Image, ImageOps, UnidentifiedImageError

from .config import PLUGIN_CONFIG
from .payload import MessageData, MessageSegmentData, SegmentType

AT_TEXT_PATTERNS = (
    re.compile(r"\[at:qq=(\d+)\]"),
    re.compile(r"\[CQ:at,qq=(\d+)\]"),
)
MAX_REPLY_DEPTH = 4
ANIMATED_IMAGE_SAMPLE_FRAMES = 5
IMAGE_COMPRESSION_QUALITIES = (85, 75, 65, 55, 45, 35, 25)
IMAGE_MIN_EDGE = 128
_qq_face_id_to_name: dict[str, str] | None = None


def _load_qq_face_map() -> dict[str, str]:
    path = Path(__file__).with_name("qq_faces.csv")
    mapping: dict[str, str] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                face_id = str(row.get("表情 ID") or "").strip()
                meaning = str(row.get("表情含义") or "").strip()
                if not face_id or not meaning:
                    continue
                mapping[face_id] = meaning
    except Exception:
        logger.opt(exception=True).warning("load qq face map failed: {}", path)
    return mapping


def _qq_face_name(face_id: str) -> str:
    global _qq_face_id_to_name
    if _qq_face_id_to_name is None:
        _qq_face_id_to_name = _load_qq_face_map()
    return _qq_face_id_to_name.get(face_id, "")


def _sender_name(event: GroupMessageEvent) -> str:
    sender = event.sender
    if sender:
        card = str(getattr(sender, "card", "") or "").strip()
        if card:
            return card
        nickname = str(getattr(sender, "nickname", "") or "").strip()
        if nickname:
            return nickname
    return str(event.user_id)


def _sender_name_from_dict(sender: dict[str, Any], user_id: int) -> str:
    card = str(sender.get("card") or "").strip()
    if card:
        return card
    nickname = str(sender.get("nickname") or "").strip()
    if nickname:
        return nickname
    return str(user_id)


def _normalize_image_name(image_name: str | None) -> str | None:
    normalized = str(image_name or "").strip()
    return normalized or None


def _replace_image_extension(image_name: str | None, suffix: str) -> str | None:
    normalized = _normalize_image_name(image_name)
    if normalized is None:
        return None
    return f"{Path(normalized).stem}{suffix}"


def _frame_image_name(
    image_name: str | None,
    *,
    frame_number: int,
    frame_count: int,
) -> str | None:
    normalized = _normalize_image_name(image_name)
    suffix = f".frame-{frame_number}-of-{frame_count}.jpg"
    if normalized is None:
        return None
    return _replace_image_extension(normalized, suffix)


def _prepare_image_for_jpeg(image: Image.Image) -> Image.Image:
    normalized = ImageOps.exif_transpose(image)
    if "A" in normalized.getbands():
        rgba_image = normalized.convert("RGBA")
        background = Image.new("RGBA", rgba_image.size, (255, 255, 255, 255))
        background.alpha_composite(rgba_image)
        return background.convert("RGB")
    if normalized.mode != "RGB":
        return normalized.convert("RGB")
    return normalized


def _encode_jpeg(image: Image.Image, *, quality: int) -> bytes:
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _encode_image_to_limit(
    image: Image.Image,
    *,
    image_name: str | None,
    max_upload_bytes: int,
) -> tuple[bytes, str, str | None] | None:
    current = _prepare_image_for_jpeg(image)
    if max_upload_bytes <= 0:
        try:
            encoded = _encode_jpeg(current, quality=IMAGE_COMPRESSION_QUALITIES[0])
        except OSError:
            logger.opt(exception=True).warning("image encode failed during compression")
            return None
        return encoded, "image/jpeg", _replace_image_extension(image_name, ".jpg")

    while True:
        for quality in IMAGE_COMPRESSION_QUALITIES:
            try:
                encoded = _encode_jpeg(current, quality=quality)
            except OSError:
                logger.opt(exception=True).warning("image encode failed during compression")
                return None
            if len(encoded) <= max_upload_bytes:
                return encoded, "image/jpeg", _replace_image_extension(image_name, ".jpg")

        width, height = current.size
        if min(width, height) <= IMAGE_MIN_EDGE:
            break
        next_width = max(IMAGE_MIN_EDGE, int(width * 0.8))
        next_height = max(IMAGE_MIN_EDGE, int(height * 0.8))
        if next_width == width and next_height == height:
            break
        current = current.resize((next_width, next_height), Image.Resampling.LANCZOS)

    return None


def _compress_image_to_limit(
    content: bytes,
    *,
    image_name: str | None,
    max_upload_bytes: int,
) -> tuple[bytes, str, str | None] | None:
    try:
        with Image.open(BytesIO(content)) as source:
            try:
                source.seek(0)
            except EOFError:
                pass
            return _encode_image_to_limit(
                source.copy(),
                image_name=image_name,
                max_upload_bytes=max_upload_bytes,
            )
    except (UnidentifiedImageError, OSError):
        logger.opt(exception=True).warning("image decode failed during compression")
        return None


def _sample_animation_frame_indexes(total_frames: int, sample_count: int) -> list[int]:
    if total_frames <= 0:
        return []
    if total_frames <= sample_count:
        return list(range(total_frames))
    if sample_count <= 1:
        return [0]

    selected: list[int] = []
    last_index = total_frames - 1
    for offset in range(sample_count):
        frame_index = round(offset * last_index / (sample_count - 1))
        if frame_index not in selected:
            selected.append(frame_index)
    if len(selected) >= sample_count:
        return selected[:sample_count]

    for frame_index in range(total_frames):
        if frame_index in selected:
            continue
        selected.append(frame_index)
        if len(selected) >= sample_count:
            break
    return selected


def _extract_animated_frame_inputs(
    content: bytes,
    *,
    image_name: str | None,
) -> list[ImageInput] | None:
    max_upload_bytes = max(0, int(PLUGIN_CONFIG.image_max_upload_bytes))
    try:
        with Image.open(BytesIO(content)) as source:
            total_frames = int(getattr(source, "n_frames", 1) or 1)
            is_animated = bool(getattr(source, "is_animated", False)) and total_frames > 1
            if not is_animated:
                return None

            frame_indexes = _sample_animation_frame_indexes(
                total_frames,
                ANIMATED_IMAGE_SAMPLE_FRAMES,
            )
            frame_inputs: list[ImageInput] = []
            for order, frame_index in enumerate(frame_indexes, start=1):
                source.seek(frame_index)
                encoded = _encode_image_to_limit(
                    source.copy(),
                    image_name=_frame_image_name(
                        image_name,
                        frame_number=order,
                        frame_count=len(frame_indexes),
                    ),
                    max_upload_bytes=max_upload_bytes,
                )
                if encoded is None:
                    return None
                frame_content, frame_mime_type, frame_name = encoded
                frame_inputs.append(
                    ImageInput(
                        base64_data=base64.b64encode(frame_content).decode("ascii"),
                        mime_type=frame_mime_type,
                        name=frame_name,
                    )
                )
    except (UnidentifiedImageError, OSError, EOFError):
        logger.opt(exception=True).warning("animated image frame extraction failed")
        return None

    if frame_inputs:
        logger.info(
            "animated image sampled frame_count={} sampled_count={} name={}",
            total_frames,
            len(frame_inputs),
            image_name or "(unknown)",
        )
    return frame_inputs or None


def _finalize_image_segment(
    content: bytes,
    *,
    mime_type: str,
    image_name: str | None,
) -> MessageSegmentData | None:
    max_upload_bytes = max(0, int(PLUGIN_CONFIG.image_max_upload_bytes))
    normalized_name = _normalize_image_name(image_name)
    animation_frames = _extract_animated_frame_inputs(
        content,
        image_name=normalized_name,
    )
    if animation_frames:
        return MessageSegmentData(
            type="image",
            image_name=normalized_name,
            image=ImageInput(
                base64_data=base64.b64encode(content).decode("ascii"),
                mime_type=mime_type,
                name=normalized_name,
                animation_frames=animation_frames,
            ),
        )

    final_content = content
    final_mime_type = mime_type
    final_name = normalized_name

    if max_upload_bytes > 0 and len(content) > max_upload_bytes:
        compressed = _compress_image_to_limit(
            content,
            image_name=normalized_name,
            max_upload_bytes=max_upload_bytes,
        )
        if compressed is None:
            logger.warning(
                "image exceeds max_upload_bytes and cannot be compressed within limit bytes={} limit={} name={}",
                len(content),
                max_upload_bytes,
                normalized_name or "(unknown)",
            )
            return None
        final_content, final_mime_type, final_name = compressed
        logger.info(
            "image compressed bytes_before={} bytes_after={} limit={} name={}",
            len(content),
            len(final_content),
            max_upload_bytes,
            final_name or normalized_name or "(unknown)",
        )

    return MessageSegmentData(
        type="image",
        image_name=final_name,
        image=ImageInput(
            base64_data=base64.b64encode(final_content).decode("ascii"),
            mime_type=final_mime_type,
            name=final_name,
        ),
    )


def _image_input_from_data_uri(uri: str, image_name: str | None) -> MessageSegmentData | None:
    if not uri.startswith("data:"):
        return None
    if "," not in uri:
        return None
    header, data = uri.split(",", 1)
    mime_type = header[5:].split(";", 1)[0].strip() or "image/png"
    try:
        content = (
            base64.b64decode(data, validate=False)
            if ";base64" in header
            else unquote_to_bytes(data)
        )
    except Exception:
        logger.warning("decode data uri image failed, skip one segment")
        return None
    return _finalize_image_segment(content, mime_type=mime_type, image_name=image_name)


async def _image_segment_to_data(bot: Bot, segment: MessageSegment) -> MessageSegmentData | None:
    file_ref = str(segment.data.get("file") or "").strip()
    image_url = str(segment.data.get("url") or "").strip()
    image_name = file_ref or None

    data_uri_segment = _image_input_from_data_uri(file_ref, image_name)
    if data_uri_segment is not None:
        return data_uri_segment

    data_uri_segment = _image_input_from_data_uri(image_url, image_name)
    if data_uri_segment is not None:
        return data_uri_segment

    content: bytes | None = None
    mime_type: str | None = None

    if False and file_ref:
        try:
            image_info = await bot.get_image(file=file_ref)
            if isinstance(image_info, dict):
                image_file = image_info.get("file")
                if isinstance(image_file, str) and image_file:
                    file_path = Path(image_file)
                    if file_path.is_file():
                        content = file_path.read_bytes()
                        image_name = image_name or file_path.name
                        guessed = mimetypes.guess_type(file_path.name)[0]
                        if guessed and guessed.startswith("image/"):
                            mime_type = guessed
                if content is None:
                    fetched_url = image_info.get("url")
                    if isinstance(fetched_url, str) and fetched_url:
                        image_url = fetched_url
        except Exception:
            logger.opt(exception=True).debug("onebot get_image failed: file={}", file_ref)

    if content is None and image_url:
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                response = await client.get(image_url)
                response.raise_for_status()
                content = response.content
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if content_type.startswith("image/"):
                    mime_type = content_type
        except Exception:
            logger.warning("download image failed, skip one segment")
            return None

    if not content:
        return None

    if not mime_type:
        guessed = mimetypes.guess_type(image_name or "")[0]
        mime_type = guessed if guessed and guessed.startswith("image/") else "image/png"

    return _finalize_image_segment(content, mime_type=mime_type, image_name=image_name)


def _normalize_message_id(raw_message_id: Any) -> int | None:
    if raw_message_id is None:
        return None
    if isinstance(raw_message_id, int):
        return raw_message_id
    text = str(raw_message_id).strip()
    if not text:
        return None
    return int(text) if text.isdigit() else None


def _coerce_to_message(raw_message: Any) -> Message:
    if isinstance(raw_message, Message):
        return raw_message
    if isinstance(raw_message, str):
        return Message(raw_message)

    normalized: list[Any]
    if isinstance(raw_message, dict):
        normalized = [raw_message]
    elif isinstance(raw_message, (list, tuple)):
        normalized = list(raw_message)
    else:
        return Message(str(raw_message or ""))

    message = Message()
    for item in normalized:
        if isinstance(item, MessageSegment):
            message.append(item)
            continue
        if isinstance(item, dict):
            segment_type = str(item.get("type") or "").strip()
            segment_data = item.get("data")
            if segment_type and isinstance(segment_data, dict):
                message.append(MessageSegment(segment_type, segment_data))
                continue
        if isinstance(item, str) and item:
            message.append(MessageSegment.text(item))

    if not message and raw_message is not None:
        return Message(str(raw_message))
    return message


def _parse_forward_sender(item: dict[str, Any], fallback_name: str) -> tuple[int, str]:
    sender_raw = item.get("sender")
    sender = sender_raw if isinstance(sender_raw, dict) else {}

    sender_id_raw = item.get("user_id", sender.get("user_id", 0))
    sender_id = int(sender_id_raw) if str(sender_id_raw).isdigit() else 0

    for candidate in (
        sender.get("card"),
        sender.get("nickname"),
        sender.get("name"),
        item.get("sender_name"),
        item.get("nickname"),
        item.get("name"),
    ):
        name = str(candidate or "").strip()
        if name:
            return sender_id, name
    if sender_id:
        return sender_id, str(sender_id)
    return 0, fallback_name


def _extract_forward_raw_message(item: dict[str, Any]) -> Any:
    if "message" in item:
        return item.get("message")

    if "content" in item:
        content = item.get("content")
        if isinstance(content, list):
            if all(
                isinstance(entry, dict) and "type" in entry and "data" in entry
                for entry in content
            ):
                return content
            if len(content) == 1 and isinstance(content[0], dict):
                nested = cast(dict[str, Any], content[0])
                if "message" in nested or "content" in nested or "segments" in nested:
                    return _extract_forward_raw_message(nested)
        return content

    if "segments" in item:
        return item.get("segments")

    if "type" in item and "data" in item:
        return [item]

    return item


async def _parse_forward_content(
    bot: Bot,
    *,
    raw_content: Any,
    group_id: int,
    message_id: int | str,
    bot_id: str,
    depth: int,
    seen: set[str],
) -> list[MessageData]:
    if not isinstance(raw_content, list):
        return []

    forward_messages: list[MessageData] = []
    for index, item in enumerate(raw_content, start=1):
        fallback_name = f"forward-{index}"
        sender_id = 0
        sender_name = fallback_name
        created_at = 0.0
        nested_message_id: int | str = f"{message_id}:forward:{index}"
        nested_raw_message: Any = item

        if isinstance(item, dict):
            nested_message_id = item.get("message_id", item.get("id", nested_message_id))
            sender_id, sender_name = _parse_forward_sender(item, fallback_name)
            created_at_raw = item.get("time", item.get("date", 0.0))
            try:
                created_at = float(created_at_raw)
            except Exception:
                created_at = 0.0
            nested_raw_message = _extract_forward_raw_message(item)

        nested_segments, _, nested_has_unknown = await _parse_segments(
            bot,
            message=_coerce_to_message(nested_raw_message),
            bot_id=bot_id,
            group_id=group_id,
            message_id=nested_message_id,
            mentioned_bot=False,
            detect_mention=False,
            depth=depth + 1,
            seen=set(seen),
        )
        forward_messages.append(
            MessageData(
                message_id=nested_message_id,
                created_at=created_at,
                sender_id=sender_id,
                sender_name=sender_name,
                mentioned_bot=False,
                segments=nested_segments,
                has_unknown_segment=nested_has_unknown,
            )
        )
        logger.debug(
            "forward entry parsed group={} message_id={} forward_index={} sender={} segment_count={} raw_type={}",
            group_id,
            message_id,
            index,
            sender_name,
            len(nested_segments),
            type(nested_raw_message).__name__,
        )
    return forward_messages


async def _load_reply_message(
    bot: Bot,
    *,
    group_id: int,
    reply_message_id: str,
    bot_id: str,
    depth: int,
    seen: set[str],
) -> MessageData | None:
    if depth > MAX_REPLY_DEPTH:
        logger.debug(
            "reply chain exceeded max depth group={} reply_message_id={} depth={}",
            group_id,
            reply_message_id,
            depth,
        )
        return None
    if reply_message_id in seen:
        logger.debug(
            "reply chain loop detected group={} reply_message_id={} depth={}",
            group_id,
            reply_message_id,
            depth,
        )
        return None

    normalized_message_id = _normalize_message_id(reply_message_id)
    if normalized_message_id is None:
        return None

    seen.add(reply_message_id)
    try:
        response = await bot.get_msg(message_id=normalized_message_id)
    except Exception:
        logger.warning(
            "get_msg failed group={} reply_message_id={} depth={}",
            group_id,
            reply_message_id,
            depth,
        )
        return None

    if not isinstance(response, dict):
        return None
    message_id = response.get("message_id", reply_message_id)
    sender = response.get("sender")
    sender_info = sender if isinstance(sender, dict) else {}
    user_id_raw = response.get("user_id", sender_info.get("user_id", 0))
    user_id = int(user_id_raw) if str(user_id_raw).isdigit() else 0
    created_at_raw = response.get("time", 0.0)
    try:
        created_at = float(created_at_raw)
    except Exception:
        created_at = 0.0

    raw_message = response.get("message")
    message_obj = _coerce_to_message(raw_message)
    segments, _, has_unknown_segment = await _parse_segments(
        bot,
        message=message_obj,
        bot_id=bot_id,
        group_id=group_id,
        message_id=message_id,
        mentioned_bot=False,
        detect_mention=False,
        depth=depth,
        seen=seen,
    )
    return MessageData(
        message_id=message_id,
        created_at=created_at,
        sender_id=user_id,
        sender_name=_sender_name_from_dict(sender_info, user_id),
        mentioned_bot=False,
        segments=segments,
        has_unknown_segment=has_unknown_segment,
    )


async def _parse_segments(
    bot: Bot,
    *,
    message: Message,
    bot_id: str,
    group_id: int,
    message_id: int | str,
    mentioned_bot: bool,
    detect_mention: bool,
    depth: int,
    seen: set[str],
) -> tuple[list[MessageSegmentData], bool, bool]:
    segments: list[MessageSegmentData] = []
    has_unknown_segment = False
    member_name_cache: dict[int, str] = {}

    async def _member_display_name(user_id: int) -> str:
        cached = member_name_cache.get(user_id)
        if cached is not None:
            return cached
        name = str(user_id)
        try:
            info = await bot.get_group_member_info(group_id=group_id, user_id=user_id)
            if isinstance(info, dict):
                card = str(info.get("card") or "").strip()
                nickname = str(info.get("nickname") or "").strip()
                name = card or nickname or name
        except Exception:
            logger.opt(exception=True).debug(
                "get_group_member_info failed group={} message_id={} user_id={}",
                group_id,
                message_id,
                user_id,
            )
        member_name_cache[user_id] = name
        return name

    for idx, segment in enumerate(message, start=1):
        segment_type = segment.type
        segment_data = dict(segment.data)
        logger.debug(
            "segment detected group={} message_id={} index={} depth={} type={} data={}",
            group_id,
            message_id,
            idx,
            depth,
            segment_type,
            segment_data,
        )

        if detect_mention:
            qq_value = segment_data.get("qq")
            if qq_value is not None:
                at_target = str(qq_value).strip()
                if at_target == bot_id:
                    mentioned_bot = True
                    logger.info(
                        "bot mention matched group={} message_id={} index={} target={}",
                        group_id,
                        message_id,
                        idx,
                        at_target,
                    )

        if segment_type == "reply":
            reply_raw_id = segment_data.get("id", segment_data.get("message_id"))
            reply_message_id = str(reply_raw_id or "").strip()
            if not reply_message_id:
                continue
            reply_message = await _load_reply_message(
                bot,
                group_id=group_id,
                reply_message_id=reply_message_id,
                bot_id=bot_id,
                depth=depth + 1,
                seen=seen,
            )
            segments.append(
                MessageSegmentData(
                    type="reply",
                    reply_message_id=reply_message_id,
                    reply=reply_message,
                    raw_data=segment_data,
                )
            )
            continue

        if segment_type in {"at", "mention"}:
            qq_value = segment_data.get("qq") or segment_data.get("user_id") or segment_data.get("id")
            target = str(qq_value or "").strip()
            if not target:
                continue

            if segments and segments[-1].type == "text" and segments[-1].text.strip() == "@":
                segments.pop()

            if target.lower() == "all":
                segments.append(
                    MessageSegmentData(
                        type="at",
                        text="@全体成员",
                        at_name="全体成员",
                        raw_data=segment_data,
                    )
                )
                continue

            user_id = int(target) if target.isdigit() else 0
            name = await _member_display_name(user_id) if user_id else target
            segments.append(
                MessageSegmentData(
                    type="at",
                    text=f"@{name}",
                    at_user_id=user_id or None,
                    at_name=str(name),
                    raw_data=segment_data,
                )
            )
            continue

        if segment_type == "text":
            text = str(segment_data.get("text", ""))
            if text:
                if detect_mention and not mentioned_bot:
                    for pattern in AT_TEXT_PATTERNS:
                        match = pattern.search(text)
                        if match and match.group(1) == bot_id:
                            mentioned_bot = True
                            logger.info(
                                "bot mention matched from text group={} message_id={} index={}",
                                group_id,
                                message_id,
                                idx,
                            )
                            break
                segments.append(
                    MessageSegmentData(
                        type="text",
                        text=text,
                        raw_data=segment_data,
                    )
                )
            continue

        if segment_type == "image":
            image_segment = await _image_segment_to_data(bot, segment)
            if image_segment is not None:
                image_segment.raw_data = segment_data
                segments.append(image_segment)
            continue

        if segment_type == "face":
            raw_face = segment_data.get("raw")
            face_text = ""
            if isinstance(raw_face, dict):
                face_text = str(raw_face.get("faceText") or "").strip()
            if face_text.startswith("/"):
                face_text = face_text[1:].strip()

            if face_text:
                name = face_text
            else:
                face_id = str(segment_data.get("id") or "").strip()
                if not face_id:
                    continue
                name = _qq_face_name(face_id) or face_id
            segments.append(
                MessageSegmentData(
                    type="face",
                    text=name,
                    raw_data=segment_data,
                )
            )
            continue

        if segment_type in {"record", "video", "file", "json", "poke", "dice", "rps"}:
            segments.append(
                MessageSegmentData(
                    type=cast(SegmentType, segment_type),
                    raw_data=segment_data,
                )
            )
            continue

        if segment_type == "forward":
            segments.append(
                MessageSegmentData(
                    type="forward",
                    raw_data=segment_data,
                    forward_messages=await _parse_forward_content(
                        bot,
                        raw_content=segment_data.get("content"),
                        group_id=group_id,
                        message_id=message_id,
                        bot_id=bot_id,
                        depth=depth,
                        seen=seen,
                    ),
                )
            )
            continue

        has_unknown_segment = True
        logger.debug(
            "unknown segment kept only in merged_text group={} message_id={} index={} depth={} type={}",
            group_id,
            message_id,
            idx,
            depth,
            segment_type,
        )

    return segments, mentioned_bot, has_unknown_segment


async def _parse_message(
    bot: Bot,
    event: GroupMessageEvent,
    bot_id: str,
) -> tuple[list[MessageSegmentData], bool, bool]:
    mentioned_bot = bool(event.is_tome())
    raw_message = str(getattr(event, "raw_message", "") or "")
    message_for_parse = _coerce_to_message(getattr(event, "original_message", event.message))
    logger.debug(
        "message meta group={} message_id={} to_me={} raw_message={}",
        event.group_id,
        event.message_id,
        mentioned_bot,
        raw_message,
    )
    if mentioned_bot:
        logger.info(
            "bot mention inferred by to_me group={} message_id={}",
            event.group_id,
            event.message_id,
        )

    event_reply = getattr(event, "reply", None)
    prefixed_reply_segment: MessageSegmentData | None = None
    has_reply_seg = False
    try:
        has_reply_seg = any(seg.type == "reply" for seg in message_for_parse)
    except Exception:
        has_reply_seg = False
    if event_reply is not None and not has_reply_seg:
        logger.debug(
            "event.reply detected group={} message_id={} reply={}",
            event.group_id,
            event.message_id,
            event_reply,
        )
        event_reply_id = str(getattr(event_reply, "message_id", "") or "").strip()
        if event_reply_id:
            nested_reply = await _load_reply_message(
                bot,
                group_id=event.group_id,
                reply_message_id=event_reply_id,
                bot_id=bot_id,
                depth=1,
                seen=set(),
            )
            if nested_reply is None:
                reply_message_obj = getattr(event_reply, "message", Message())
                reply_sender = getattr(event_reply, "sender", None)
                reply_sender_dict = {}
                if reply_sender is not None and hasattr(reply_sender, "model_dump"):
                    reply_sender_dict = reply_sender.model_dump()
                reply_user_id_raw = reply_sender_dict.get("user_id", 0)
                reply_user_id = int(reply_user_id_raw) if str(reply_user_id_raw).isdigit() else 0
                fallback_segments, _, fallback_has_unknown = await _parse_segments(
                    bot,
                    message=_coerce_to_message(reply_message_obj),
                    bot_id=bot_id,
                    group_id=event.group_id,
                    message_id=event_reply_id,
                    mentioned_bot=False,
                    detect_mention=False,
                    depth=1,
                    seen={event_reply_id},
                )
                nested_reply = MessageData(
                    message_id=event_reply_id,
                    created_at=float(getattr(event_reply, "time", 0)),
                    sender_id=reply_user_id,
                    sender_name=_sender_name_from_dict(reply_sender_dict, reply_user_id),
                    mentioned_bot=False,
                    segments=fallback_segments,
                    has_unknown_segment=fallback_has_unknown,
                )
            prefixed_reply_segment = MessageSegmentData(
                type="reply",
                reply_message_id=event_reply_id,
                reply=nested_reply,
            )

    parsed_segments, parsed_mentioned_bot, parsed_has_unknown = await _parse_segments(
        bot,
        message=message_for_parse,
        bot_id=bot_id,
        group_id=event.group_id,
        message_id=event.message_id,
        mentioned_bot=mentioned_bot,
        detect_mention=True,
        depth=0,
        seen=set(),
    )
    if prefixed_reply_segment is not None and not any(
        segment.type == "reply" for segment in parsed_segments
    ):
        parsed_segments.insert(0, prefixed_reply_segment)
    return parsed_segments, parsed_mentioned_bot, parsed_has_unknown
