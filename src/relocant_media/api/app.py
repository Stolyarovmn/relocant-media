from dataclasses import asdict
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.runtime import TelegramRuntime
from relocant_media.bot.updates import classify_update, update_id
from relocant_media.config import settings
from relocant_media.storage.db import SessionFactory

app = FastAPI(title="Relocant Media API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, object]:
    expected = settings.telegram_webhook_secret
    if expected and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=401, detail="invalid Telegram webhook secret")

    payload: dict[str, Any] = await request.json()
    kind = classify_update(payload)
    if not settings.telegram_runtime_enabled:
        return {"ok": True, "update_id": update_id(payload), "kind": kind.value}

    if not expected:
        raise HTTPException(status_code=503, detail="Telegram webhook secret is not configured")
    if not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="Telegram bot token is not configured")

    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0)) as client:
        bot = TelegramBotAPI(settings.telegram_bot_token, client)
        with SessionFactory() as session:
            runtime = TelegramRuntime(
                session,
                bot,
                city=settings.city_id,
                staging_discussion_group_id=settings.telegram_staging_discussion_group_id,
            )
            result = await runtime.process(payload)

    response = asdict(result)
    response["kind"] = result.kind.value
    return {"ok": True, **response}
