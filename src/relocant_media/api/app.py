from fastapi import FastAPI, Header, HTTPException, Request

from relocant_media.config import settings

app = FastAPI(title="Relocant Media API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.app_env}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    expected = settings.telegram_webhook_secret
    if expected and x_telegram_bot_api_secret_token != expected:
        raise HTTPException(status_code=401, detail="invalid Telegram webhook secret")

    # Parsing/dispatch is deliberately not implemented until Telegram resources
    # and update subscriptions are finalized. Returning success prevents retries
    # in local integration tests while keeping side effects disabled.
    await request.json()
    return {"ok": True}
