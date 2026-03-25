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


def _parenthetical_action_rule_text() -> str:
    return "- 注意：你的输出仅需要包含发送在qq群对话框中的对话信息，禁止添加任何动作、状态描述，尤其是放在括号里的小动作。"


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
    parenthetical_action_rule_text = _parenthetical_action_rule_text()
    blacklisted_parenthetical_keywords: tuple[str, ...] = (
        "拍",
        "递",
        "捂",
        "擦",
        "晃",
        "敲",
        "挥",
        "躲",
        "低头",
        "抬头",
        "歪头",
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
        "拍肩",
        "递零食",
        "递奶茶",
        "递咖啡",
        "困惑脸",
        "捂脸",
        "小声",
        "跺"
    )
    parenthetical_segment_pattern = re.compile(r"（([^（）]{1,16})）|\(([^()]{1,16})\)")
    first_match: tuple[int, str, str] | None = None
    for match in parenthetical_segment_pattern.finditer(text):
        content = match.group(1) or match.group(2) or ""
        if not content:
            continue
        for keyword in blacklisted_parenthetical_keywords:
            if keyword not in content:
                continue
            if first_match is None or match.start() < first_match[0]:
                first_match = (match.start(), match.group(0), keyword)
            break

    if first_match is None:
        return LiteReplyCheckResult(
            checker_name="parenthetical_blacklist",
            passed=True,
        )

    offset, phrase, keyword = first_match
    diagnostics = (
        f"error[LITE002]: 第{offset + 1}个字附近出现黑名单括号表达“{phrase}”，命中关键词“{keyword}”\n"
        "原因：\n"
        f"{parenthetical_action_rule_text}"
    )
    return LiteReplyCheckResult(
        checker_name="parenthetical_blacklist",
        passed=False,
        diagnostics=diagnostics,
    )


def check_reply_lite_sentence_final_parenthetical(text: str) -> LiteReplyCheckResult:
    parenthetical_action_rule_text = _parenthetical_action_rule_text()
    sentence_final_parenthetical_pattern = re.compile(
        r"（[^（）]{1,16}）(?=[ \t]*(?:$|\n))|\([^()]{1,16}\)(?=[ \t]*(?:$|\n))"
    )
    for match in sentence_final_parenthetical_pattern.finditer(text):
        diagnostics = (
            f"error[LITE003]: 第{match.start() + 1}个字附近出现句尾括号表达“{match.group(0)}”\n"
            "原因：\n"
            f"{parenthetical_action_rule_text}\n"
            "如果你确实需要使用括号来对内容进行补充、解释，请在括号后补一个句号或其他标点。"
        )
        return LiteReplyCheckResult(
            checker_name="sentence_final_parenthetical",
            passed=False,
            diagnostics=diagnostics,
        )

    return LiteReplyCheckResult(
        checker_name="sentence_final_parenthetical",
        passed=True,
    )


DEFAULT_LITE_REPLY_CHECKERS: tuple[LiteReplyChecker, ...] = (
    check_reply_lite_opening_this,
    check_reply_lite_parenthetical_blacklist,
    check_reply_lite_sentence_final_parenthetical,
)
