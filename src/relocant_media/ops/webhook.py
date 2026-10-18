import asyncio
import json

import httpx

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.config import settings, telegram_setup_errors


async def _configure() -> None:
    errors = telegram_setup_errors(settings)
    if errors:
        raise RuntimeError("; ".join(errors))

    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        bot = TelegramBotAPI(settings.telegram_bot_token, client)
        await bot.set_webhook(settings.telegram_webhook_url, settings.telegram_webhook_secret)


def main() -> None:
    try:
        asyncio.run(_configure())
    except (httpx.HTTPError, RuntimeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        raise SystemExit(1) from error
    print(json.dumps({"ok": True, "webhook_configured": True}))


if __name__ == "__main__":
    main()
