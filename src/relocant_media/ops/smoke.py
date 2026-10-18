import argparse
import json
from typing import Any, cast

import httpx

from relocant_media.config import settings, telegram_setup_errors


class SmokeCheckError(RuntimeError):
    pass


def _json_object(response: httpx.Response, label: str) -> dict[str, Any]:
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise SmokeCheckError(f"{label} returned a non-object JSON response")
    return cast(dict[str, Any], payload)


def _telegram_call(client: httpx.Client, method: str, payload: dict[str, object]) -> object:
    response = client.post(
        f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}",
        json=payload,
    )
    data = _json_object(response, f"Telegram {method}")
    if data.get("ok") is not True:
        raise SmokeCheckError(f"Telegram {method} failed: {data.get('description')}")
    return data.get("result")


def check_api(client: httpx.Client, base_url: str) -> dict[str, object]:
    base = base_url.rstrip("/")
    health = _json_object(client.get(f"{base}/health"), "health")
    readiness_response = client.get(f"{base}/ready")
    readiness = readiness_response.json()
    if readiness_response.status_code != 200:
        raise SmokeCheckError(f"readiness failed: {readiness}")
    if not isinstance(readiness, dict):
        raise SmokeCheckError("readiness returned a non-object JSON response")
    return {"health": health, "readiness": readiness}


def check_telegram(client: httpx.Client) -> dict[str, object]:
    errors = telegram_setup_errors(settings)
    if errors:
        raise SmokeCheckError("; ".join(errors))

    bot = _telegram_call(client, "getMe", {})
    chats: dict[str, object] = {}
    for label, chat_id in (
        ("channel", settings.telegram_staging_channel_id),
        ("discussion_group", settings.telegram_staging_discussion_group_id),
        ("forum_group", settings.telegram_staging_forum_group_id),
    ):
        chats[label] = _telegram_call(client, "getChat", {"chat_id": chat_id})

    webhook = _telegram_call(client, "getWebhookInfo", {})
    if not isinstance(webhook, dict):
        raise SmokeCheckError("Telegram getWebhookInfo returned an invalid result")
    webhook_url = webhook.get("url")
    if webhook_url != settings.telegram_webhook_url:
        raise SmokeCheckError(
            "Telegram webhook URL does not match TELEGRAM_WEBHOOK_URL; configure it explicitly"
        )

    return {
        "bot": bot,
        "chats": chats,
        "webhook": {"url_matches": True},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run non-publishing staging smoke checks")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()

    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            result: dict[str, object] = {"api": check_api(client, args.base_url)}
            if args.telegram:
                result["telegram"] = check_telegram(client)
    except (httpx.HTTPError, SmokeCheckError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from error

    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
