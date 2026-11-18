"""Уведомления владельцу об инцидентах предложки; без метрик времени реакции."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any

import httpx

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.ops.vercel_inbox import _request

ORIGIN = "https://relocant-reader-intake.vercel.app"
MESSAGES = {
    "unavailable": "Приёмник или его хранилище недоступны. Новые сообщения могут ждать доставки.",
    "queue_waiting": "Материалы сохранены, но больше 20 минут ждут канонического подтверждения.",
    "uncertain_delivery": "Результат отправки подтверждения неизвестен. "
                          "Нужна сверка; повтор отключён.",
    "storage_near_capacity": "Транспортное хранилище приближается к установленному пределу.",
    "attempt_stuck": "Попытка обработки не завершилась. Нужна проверка сохранённого события.",
}
FLAGS = set(MESSAGES) - {"unavailable"}
LINK = "https://github.com/Stolyarovmn/relocant-media/actions/workflows/editorial-actions.yml"


def inspect(token: str, *, client: httpx.Client | None = None) -> list[str] | None:
    """None means an older receiver: do not invent a production incident during rollout."""
    try:
        status = _request(ORIGIN, token, {"operation": "status"}, client)
        health = status.get("intake_health")
        if health is None:
            return None
        if (not isinstance(health, dict) or type(health.get("version")) is not int
                or health["version"] != 1 or any(type(health.get(k)) is not bool for k in FLAGS)):
            return ["unavailable"]
        return sorted(k for k in FLAGS if health[k])
    except Exception:
        # No response bodies, exception strings, user content or tokens in alerts/logs.
        return ["unavailable"]


def save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf8")
    tmp.replace(path)
    if output := os.environ.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf8") as stream:
            stream.write("changed=true\n")


async def send(text: str) -> None:
    chat_id = int(os.environ["EDITORIAL_OWNER_CHAT_ID"])
    if chat_id <= 0 or chat_id != int(os.environ["EDITORIAL_OWNER_USER_ID"]):
        raise ValueError("private owner required")
    async with httpx.AsyncClient(timeout=18, trust_env=False) as client:
        bot = TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], client)
        await bot.send_message(chat_id, text)


async def check_once(
    path: Path, *, dry_run: bool, test_notification: bool = False,
) -> dict[str, Any]:
    prior = json.loads(path.read_text(encoding="utf8")) if path.exists() else {}
    if test_notification:
        if os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise ValueError("manual notification test required")
        key = os.environ["GITHUB_RUN_ID"]
        if prior.get("test_run") == key:
            return {"status": "test_already_attempted"}
        state = {**prior, "test_run": key, "test_delivery": "sending"}
        text = "Проверка уведомлений предложки: канал оповещений работает. Это тест, сбоя нет."
    else:
        issues = inspect(os.environ.get("EDITORIAL_INGRESS_TOKEN", ""))
        if issues is None:
            return {"status": "receiver_upgrade_pending"}
        if dry_run:
            return {"status": "checked", "issues": issues, "dry_run": True}
        previous = prior.get("issues", [])
        if issues == previous:
            return {"status": "unchanged", "issues": issues}
        state = {**prior, "issues": issues, "delivery": "sending"}
        text = ("Предложка: обнаружен сбой.\n" + "\n".join(MESSAGES[k] for k in issues)
                if issues else "Предложка: проверка снова проходит, приёмник доступен. "
                "Ранее неподтверждённые отправки требуют отдельной сверки.")
    if dry_run:
        return {"status": "test_preview", "dry_run": True}
    field = "test_delivery" if test_notification else "delivery"
    # Persist the attempt before Telegram. Unknown sends are never automatically retried.
    save(path, state)
    try:
        await send(text + "\n" + LINK)
    except Exception:
        state[field] = "uncertain"
    else:
        state[field] = "sent"
    save(path, state)
    return {"status": state[field], "issues": state.get("issues", []),
            "test": test_notification}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(check_once(args.state, dry_run=args.dry_run,
                                       test_notification=args.test_notification))
        print(json.dumps({"intake_watchdog": result}, ensure_ascii=False))
    except Exception as error:
        print(json.dumps({"intake_watchdog_error": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
