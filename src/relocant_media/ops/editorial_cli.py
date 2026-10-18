"""One-shot persisted editorial cycle; Telegram delivery is opt-in and staging-only."""

import argparse
import asyncio
import json
import re
from dataclasses import asdict

import httpx

from relocant_media.ai.gemini import GeminiStructuredProvider
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.publisher import StagingTelegramPublisher
from relocant_media.config import settings
from relocant_media.services.editorial import EditorialService
from relocant_media.storage.db import SessionFactory


async def _run(raw_item_id: int, send_staging: bool, transport: str) -> dict[str, object]:
    if settings.app_env.lower() in {"prod", "production"}:
        raise ValueError("editorial engineering CLI must not run in production")
    if send_staging and transport != "bot-api":
        raise ValueError("--send-staging requires --transport bot-api")
    # Gemini credentials are independent of Telegram and MTProto credentials.
    pipeline = EditorialPipeline(GeminiStructuredProvider(
        settings.gemini_api_key, model=settings.gemini_model,
    ))
    with SessionFactory() as session:
        service = EditorialService(session, pipeline)
        run = service.prepare(raw_item_id)
        result: dict[str, object] = {
            "raw_item_id": run.raw_item_id, "outcome": run.outcome,
            "post_id": run.post_id, "transport": transport,
        }
        if run.outcome["status"] != "ready":
            result["delivery"] = "policy_blocked"
            return result
        if not send_staging:
            preview = await service.deliver(raw_item_id)
            if transport == "fake":
                # Exercise the actual Bot API serialization/parser with an in-memory
                # HTTP transport. Never consume a durable real-delivery receipt.
                requests: list[dict[str, object]] = []

                def fake_send(request: httpx.Request) -> httpx.Response:
                    requests.append(json.loads(request.content))
                    return httpx.Response(200, json={
                        "ok": True, "result": {"message_id": 1},
                    })

                async with httpx.AsyncClient(transport=httpx.MockTransport(fake_send)) as client:
                    publisher = StagingTelegramPublisher(
                        TelegramBotAPI("fake-token", client), "-100000000001",
                    )
                    await publisher.publish(preview.text)
                result["fake_requests"] = requests
            result["delivery"] = asdict(preview)
            return result
        if not re.fullmatch(r"-100[0-9]+", settings.telegram_channel_id):
            raise ValueError("numeric production channel id is required to exclude its target")
        if not re.fullmatch(r"-100[0-9]+", settings.telegram_staging_channel_id):
            raise ValueError("staging channel must use a numeric -100 chat id")
        async with httpx.AsyncClient(timeout=30) as client:
            publisher = StagingTelegramPublisher(
                TelegramBotAPI(settings.telegram_bot_token, client),
                settings.telegram_staging_channel_id,
                settings.telegram_channel_id,
            )
            result["delivery"] = asdict(await service.deliver(
                raw_item_id, publisher, dry_run=False,
            ))
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a safe persisted editorial cycle")
    parser.add_argument("--raw-item-id", type=int, required=True)
    parser.add_argument("--transport", choices=("dry-run", "fake", "bot-api"), default="dry-run")
    parser.add_argument("--send-staging", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(
        args.raw_item_id, args.send_staging, args.transport,
    )), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
