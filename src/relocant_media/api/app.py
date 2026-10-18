from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from relocant_media.bot.updates import classify_update, update_id
from relocant_media.config import settings

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
    return {"ok": True, "update_id": update_id(payload), "kind": kind.value}
