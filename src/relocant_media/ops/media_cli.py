"""Review-bound media preview/fake transport; real staging requires an explicit flag."""

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.bot.media import MediaRelease
from relocant_media.bot.media_publisher import StagingMediaPublisher
from relocant_media.services.media_delivery import deliver_media_release


async def run(
    release_path: Path, assets: Path, receipts: Path, transport: str, send_staging: bool,
) -> dict[str, object]:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging required")
    if send_staging != (transport == "bot-api"):
        raise ValueError("real staging requires --transport bot-api --send-staging")
    production = os.environ.get("TELEGRAM_CHANNEL_ID", "")
    target = os.environ.get("TELEGRAM_STAGING_CHANNEL_ID", "")
    release = MediaRelease.model_validate_json(release_path.read_text(encoding="utf-8"))
    if not production.startswith("-100") or not production[4:].isdigit():
        raise ValueError("numeric production exclusion required")
    if not target or target != release.target_id or target == production:
        raise ValueError("explicit staging target must match release")
    database = os.environ.get("DATABASE_URL")
    if not database:
        raise ValueError("explicit DATABASE_URL required")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "") if send_staging else "fake-token"
    if not token:
        raise ValueError("bot token required")
    calls = []

    def fake_send(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path.rsplit("/", 1)[-1])
        result: object = [{"message_id": 100 + i} for i in range(len(release.assets))]
        if len(release.assets) == 1:
            result = {"message_id": 100}
        return httpx.Response(200, json={"ok": True, "result": result})

    engine = create_engine(database)
    try:
        async with httpx.AsyncClient(
            transport=None if send_staging else httpx.MockTransport(fake_send),
            timeout=30, trust_env=False, follow_redirects=False,
        ) as client:
            publisher = StagingMediaPublisher(TelegramBotAPI(token, client), target, production)
            with Session(engine) as session:
                result = await deliver_media_release(
                    session, release, assets=assets, receipts=receipts, publisher=publisher,
                    production_id=production, dry_run=not send_staging,
                )
                if not send_staging:
                    from relocant_media.bot.api import PhotoUpload
                    from relocant_media.bot.media import validate_media

                    paths = validate_media(release, assets)
                    uploads = []
                    import hashlib

                    for path, asset in zip(paths, release.assets, strict=True):
                        data = path.read_bytes()
                        if hashlib.sha256(data).hexdigest() != asset.sha256:
                            raise ValueError("media changed before fake upload")
                        uploads.append(PhotoUpload(path.name, data))
                    fake_ids = await publisher.publish(release.caption, tuple(uploads))
                    return {**result, "mode": "fake", "fake_message_ids": fake_ids,
                            "fake_requests": calls, "real_messages_sent": 0}
                return {**result, "mode": "real_staging"}
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview or explicitly send reviewed staging media",
    )
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--transport", choices=("fake", "bot-api"), default="fake")
    parser.add_argument("--send-staging", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(
            args.release, args.assets, args.receipts, args.transport, args.send_staging,
        ))
    except Exception:
        parser.exit(1, "Медиа-доставка остановлена: проверьте пакет и квитанцию перед повтором.\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
