from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class LiteReplyCheckResult:
    checker_name: str
    passed: bool
    diagnostics: str = ""


LiteReplyChecker = Callable[[str], LiteReplyCheckResult]


def _strip_leading_tone_words(text: str) -> tuple[str, int]:
    leading_tone_word_pattern = r"(?:哈+|啊|诶|哎|好家伙|呜+|前辈)"
    leading_tone_prefix_pattern = re.compile(
        rf"^(?:\s*(?:{leading_tone_word_pattern})\s*[，！。？]*\s*)+"
    )
    raw = text.lstrip()
    leading_whitespace = len(text) - len(raw)
    match = leading_tone_prefix_pattern.match(raw)
    consumed = leading_whitespace
    if match is not None:
        consumed += match.end()
    return text[consumed:], consumed


def check_reply_lite_opening_this(text: str) -> LiteReplyCheckResult:
    repost_content_rule_text = (
        "- 对于群友转载的图片、文字等内容（包括但不限于💩），你不得将此消息本身作为主语、宾语或其他任何句子成分，你应该直接针对消息内容中出现的人、事、物进行讨论\n"
        "- 如果新消息中包含多张图片、多段文字，你可以对消息中的人事物添加描述以消除歧义。你仍然不得以消息本身作为主语。\n"
        "- 禁止以“这”作为消息的开头。"
    )
    remaining_text, offset = _strip_leading_tone_words(text)
    if not remaining_text.startswith("这"):
        return LiteReplyCheckResult(
            checker_name="opening_this",
            passed=True,
        )

    diagnostics = (
        f"error[LITE001]: 第{offset + 1}个字不能以“这”开头\n"
        "原因：\n"
        f"{repost_content_rule_text}"
    )
    return LiteReplyCheckResult(
        checker_name="opening_this",
        passed=False,
        diagnostics=diagnostics,
    )


def check_reply_lite_parenthetical_blacklist(text: str) -> LiteReplyCheckResult:
    parenthetical_action_rule_text = (
        "- 注意：你的输出仅需要包含发送在qq群对话框中的对话信息，禁止添加任何动作、状态描述，尤其是放在括号里的小动作。"
    )
    blacklisted_parenthetical_phrases: tuple[str, ...] = (
        "（拍肩）",
        "(拍肩)",
        "（递零食）",
        "(递零食)",
        "（递奶茶）",
        "(递奶茶)",
        "（递咖啡）",
        "(递咖啡)",
        "（困惑脸）",
        "(困惑脸)",
        "（捂脸）",
        "(捂脸)",
        "（小声）",
        "(小声)",
    )
    first_match: tuple[int, str] | None = None
    for phrase in blacklisted_parenthetical_phrases:
        position = text.find(phrase)
        if position < 0:
            continue
        if first_match is None or position < first_match[0]:
            first_match = (position, phrase)

    if first_match is None:
        return LiteReplyCheckResult(
            checker_name="parenthetical_blacklist",
            passed=True,
        )

    offset, phrase = first_match
    diagnostics = (
        f"error[LITE002]: 第{offset + 1}个字附近出现黑名单表达“{phrase}”\n"
        "原因：\n"
        f"{parenthetical_action_rule_text}"
    )
    return LiteReplyCheckResult(
        checker_name="parenthetical_blacklist",
        passed=False,
        diagnostics=diagnostics,
    )


def _is_high_confidence_parenthetical_match(text: str, start: int, end: int, content: str) -> bool:
    high_confidence_parenthetical_token_blacklist: tuple[str, ...] = (
        "拍",
        "递",
        "捂",
        "擦",
        "晃",
        "敲",
        "挥",
        "低头",
        "抬头",
        "歪头",
        "小声",
        "困惑",
        "无辜",
        "心虚",
        "委屈",
        "肩",
        "脸",
        "嘴",
        "胸口",
        "桌",
        "手",
        "认错",
        "叹气",
    )
    parenthetical_safe_non_english_pattern = re.compile(
        r"[0-9%/+._:@#-]|https?://|www\."
    )
    parenthetical_pure_english_pattern = re.compile(r"^[A-Za-z][A-Za-z\s]{0,7}$")
    parenthetical_left_context_allowed = "，。！？；：、,.!?\n"
    if parenthetical_safe_non_english_pattern.search(content):
        return False
    if parenthetical_pure_english_pattern.fullmatch(content):
        return False
    if not any(token in content for token in high_confidence_parenthetical_token_blacklist):
        return False

    left_context = text[:start].rstrip()
    right_context = text[end:].lstrip()

    left_ok = not left_context or left_context[-1] in parenthetical_left_context_allowed
    right_ok = not right_context or right_context[0] in parenthetical_left_context_allowed
    return left_ok or right_ok


def check_reply_lite_parenthetical_token_blacklist(text: str) -> LiteReplyCheckResult:
    parenthetical_action_rule_text = (
        "- 注意：你的输出仅需要包含发送在qq群对话框中的对话信息，禁止添加任何动作、状态描述，尤其是放在括号里的小动作。"
    )
    high_confidence_parenthetical_token_blacklist: tuple[str, ...] = (
        "拍",
        "递",
        "捂",
        "擦",
        "晃",
        "敲",
        "挥",
        "低头",
        "抬头",
        "歪头",
        "小声",
        "困惑",
        "无辜",
        "心虚",
        "委屈",
        "肩",
        "脸",
        "嘴",
        "胸口",
        "桌",
        "手",
        "认错",
        "叹气",
    )
    parenthetical_segment_pattern = re.compile(r"（([^（）]{2,8})）|\(([^()]{2,8})\)")
    for match in parenthetical_segment_pattern.finditer(text):
        content = match.group(1) or match.group(2) or ""
        if not content:
            continue
        if not _is_high_confidence_parenthetical_match(
            text,
            match.start(),
            match.end(),
            content,
        ):
            continue

        matched_token = next(
            token
            for token in high_confidence_parenthetical_token_blacklist
            if token in content
        )
        diagnostics = (
            f"error[LITE003]: 第{match.start() + 1}个字附近出现高风险括号表达“{match.group(0)}”，"
            f"命中词典词“{matched_token}”\n"
            "原因：\n"
            f"{parenthetical_action_rule_text}"
        )
        return LiteReplyCheckResult(
            checker_name="parenthetical_token_blacklist",
            passed=False,
            diagnostics=diagnostics,
        )

    return LiteReplyCheckResult(
        checker_name="parenthetical_token_blacklist",
        passed=True,
    )


DEFAULT_LITE_REPLY_CHECKERS: tuple[LiteReplyChecker, ...] = (
    check_reply_lite_opening_this,
    check_reply_lite_parenthetical_blacklist,
    check_reply_lite_parenthetical_token_blacklist,
)
