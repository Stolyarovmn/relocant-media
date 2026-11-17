from asyncio import Lock
from dataclasses import asdict
from hmac import compare_digest
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response

from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI
from relocant_media.bot.runtime import TelegramRuntime
from relocant_media.bot.updates import classify_update, update_id
from relocant_media.config import settings
from relocant_media.ops.readiness import build_readiness
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.private_intake import process_private_updates
from relocant_media.storage.db import SessionFactory, engine

app = FastAPI(title="Relocant Media API", version="0.1.0")
_intake_lock = Lock()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}


@app.get("/ready")
def ready(response: Response) -> dict[str, object]:
    snapshot = build_readiness(settings, engine)
    if snapshot["status"] != "ready":
        response.status_code = 503
    return snapshot


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

    response_payload = asdict(result)
    response_payload["kind"] = result.kind.value
    return {"ok": True, **response_payload}


@app.post("/telegram/intake-webhook")
async def telegram_intake_webhook(
    payload: dict[str, Any],
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict[str, object]:
    if settings.app_env.lower() != "staging" or not settings.telegram_runtime_enabled:
        raise HTTPException(status_code=404)
    secret = settings.telegram_webhook_secret
    if not secret or not settings.telegram_bot_token:
        raise HTTPException(status_code=503, detail="staging Telegram webhook is not configured")
    try:
        staging_channel = int(settings.telegram_staging_channel_id)
    except ValueError:
        raise HTTPException(status_code=503, detail="staging channel is not configured") from None
    if staging_channel >= 0 or staging_channel == APPROVED_CHANNEL_ID or (
        settings.telegram_staging_channel_id == settings.telegram_channel_id
    ):
        raise HTTPException(status_code=503, detail="a separate staging channel is required")
    if not compare_digest(secret.encode(), x_telegram_bot_api_secret_token.encode()):
        raise HTTPException(status_code=401, detail="invalid Telegram webhook secret")
    uid = update_id(payload)
    if uid is None or uid < 0:
        raise HTTPException(status_code=400, detail="invalid Telegram update ID")

    # The staging runner has one process. The lock keeps concurrent retries from
    # claiming the same draft or trying to send the same receipt.
    async with _intake_lock, httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
        bot = TelegramBotAPI(settings.telegram_bot_token, client)
        try:
            await process_private_updates(
                engine, bot, [payload], reader_feedback_enabled=True, channel_id=staging_channel,
            )
        except (httpx.HTTPError, TelegramAPIError):
            raise HTTPException(
                status_code=503, detail="Telegram is temporarily unavailable",
            ) from None
    return {"ok": True, "update_id": uid}
