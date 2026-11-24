"""Join the original Telegram messages of a private submission."""

import re
from typing import Any

NEW_CASE_LABEL = "Новое обращение"
_GREETING = re.compile(
    r"(?:здравствуйте|здравствуй|привет|приветствую|доброе утро|добрый день|добрый вечер)"
    r"[\s.!?,…👋]*",
    re.IGNORECASE,
)
_PRESENCE_CHECK = re.compile(
    r"(?:есть\s+кто(?:[- ]нибудь)?|кто(?:[- ]нибудь)?\s+(?:есть|тут|здесь)|"
    r"(?:вы|ты)\s+(?:тут|здесь)|алло|ау)[\s.!?,…]*", re.IGNORECASE,
)


def is_start_text(text: str) -> bool:
    return (text == NEW_CASE_LABEL or text.partition(" ")[0].split("@")[0] == "/start"
            or _GREETING.fullmatch(text) is not None
            or _PRESENCE_CHECK.fullmatch(text) is not None)


def combine_reader_parts(parts: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    ordered = [parts[key] for key in sorted(parts, key=int)]
    text = "\n".join(part["text"] for part in ordered if part["text"] and not (
        not part["attachments"] and not part.get("forwarded") and is_start_text(part["text"])
    ))
    attachments: dict[str, dict[str, Any]] = {}
    for part in ordered:
        for media in part["attachments"]:
            attachments[media.get("file_unique_id") or media["file_id"]] = media
    return text, list(attachments.values())
