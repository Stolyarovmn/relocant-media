import asyncio
import json
from urllib.parse import urlsplit

import httpx

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.config import settings, telegram_setup_errors


async def _configure() -> None:
    errors = telegram_setup_errors(settings)
    reader_intake = urlsplit(settings.telegram_webhook_url).path == "/telegram/intake-webhook"
    if reader_intake and (settings.app_env.lower() != "staging"
                          or not settings.telegram_runtime_enabled):
        errors.append("reader intake webhook requires the staging Telegram runtime")
    if errors:
        raise RuntimeError("; ".join(errors))

    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        bot = TelegramBotAPI(settings.telegram_bot_token, client)
        await bot.set_webhook(
            settings.telegram_webhook_url, settings.telegram_webhook_secret,
            reader_intake=reader_intake,
        )


def main() -> None:
    try:
        asyncio.run(_configure())
    except httpx.HTTPError:
        print(json.dumps({"ok": False, "error": "failed to contact Telegram"}, ensure_ascii=False))
        raise SystemExit(1) from None
    except (RuntimeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from error
    print(json.dumps({"ok": True, "webhook_configured": True}))


if __name__ == "__main__":
    main()
