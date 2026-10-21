"""Owner-selected title presentation, fixed before independent review."""

import os

TEST_PREFIX = "[тестовый запуск]"


def channel_mode() -> str:
    mode = os.environ.get("EDITORIAL_CHANNEL_MODE", "test")
    if mode not in {"test", "public"}:
        raise ValueError("invalid editorial channel mode")
    return mode


def presentation_title(title: str) -> str:
    bare = title.removeprefix(TEST_PREFIX).strip()
    return f"{TEST_PREFIX} {bare}" if channel_mode() == "test" else bare


def valid_title_presentation(title: str) -> bool:
    if not title.strip():
        return False
    if channel_mode() == "public":
        return TEST_PREFIX not in title
    return title.startswith(TEST_PREFIX) and bool(title.removeprefix(TEST_PREFIX).strip())


def title_instruction() -> str:
    if channel_mode() == "public":
        return ("Канал работает в публичном режиме: заголовок начинается с самой новости. "
                "Пометку [тестовый запуск] не добавляй: владелец отменил её. ")
    return ("Префикс [тестовый запуск] — обязательная редакционная пометка владельца, "
            "а не утверждение об описываемом событии. Его отсутствие в источнике не является "
            "ошибкой: не требуй убрать эту пометку. ")
