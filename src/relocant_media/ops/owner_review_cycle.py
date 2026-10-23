"""Five-minute owner-review worker for high-risk editorial packages."""

from __future__ import annotations

import asyncio
import json
import os
import traceback
from pathlib import Path

import httpx
from google import genai
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from relocant_media.ai.gemini import GeminiStructuredProvider
from relocant_media.bot.api import TelegramBotAPI
from relocant_media.ops.server_cycle import ChannelPublisher, ServerAI
from relocant_media.ops.server_runtime import fetch_documents
from relocant_media.services.owner_review import (
    OWNER_REVIEW_KIND,
    process_owner_actions,
    queue_owner_reviews,
)
from relocant_media.services.owner_review_prepare import (
    pending_high_risk_holds,
    prepare_high_risk_holds,
)
from relocant_media.storage.file_lock import file_lock
from relocant_media.storage.models import Submission
from relocant_media.storage.sqlite_schema import verify_sqlite_schema


def _positive_int(name: str) -> int:
    value = os.environ.get(name, "")
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive integer") from error
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _revision_count(engine: Engine) -> int:
    with Session(engine) as session:
        return int(session.scalar(
            select(func.count())
            .select_from(Submission)
            .where(
                Submission.kind == OWNER_REVIEW_KIND,
                Submission.status == "revision_requested",
            )
        ) or 0)


def _provider(engine: Engine) -> ServerAI | None:
    if not _revision_count(engine) and not pending_high_risk_holds(engine):
        return None
    api_key = os.environ["GEMINI_API_KEY"]
    model = os.environ["GEMINI_MODEL"]
    client = genai.Client(
        api_key=api_key,
        http_options={"timeout": 45000, "retry_options": {"attempts": 0}},
    )
    return ServerAI(GeminiStructuredProvider(api_key, model=model, client=client))


async def run_cycle(
    engine: Engine,
    *,
    owner_user_id: int,
    owner_chat_id: int,
) -> dict[str, object]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    provider = _provider(engine)
    preparation = (
        prepare_high_risk_holds(engine, provider, fetch_documents)
        if provider is not None and pending_high_risk_holds(engine)
        else {"prepared": 0, "blocked": 0}
    )
    async with httpx.AsyncClient(
        timeout=30,
        trust_env=False,
        transport=httpx.AsyncHTTPTransport(retries=0),
    ) as client:
        bot = TelegramBotAPI(token, client)
        publisher = ChannelPublisher(bot)
        actions = await process_owner_actions(
            engine,
            bot,
            owner_user_id=owner_user_id,
            owner_chat_id=owner_chat_id,
            provider=provider,
            live_document_fetcher=fetch_documents,
            publisher=publisher,
            publish_enabled=os.environ.get("EDITORIAL_SEND_ENABLED") == "true",
        )
        queued = await queue_owner_reviews(
            engine,
            bot,
            owner_user_id=owner_user_id,
            owner_chat_id=owner_chat_id,
            enabled=True,
        )
    return {
        "status": "ready",
        "preparation": preparation,
        "actions": actions,
        "queue": queued,
    }


def main() -> None:
    if os.environ.get("EDITORIAL_OWNER_REVIEW_ENABLED") != "true":
        print(json.dumps({"status": "disabled"}, ensure_ascii=False))
        return
    owner_user_id = _positive_int("EDITORIAL_OWNER_USER_ID")
    owner_chat_id = _positive_int("EDITORIAL_OWNER_CHAT_ID")
    url = make_url(os.environ["DATABASE_URL"])
    if (
        url.get_backend_name() != "sqlite"
        or not url.database
        or url.query
        or not Path(url.database).is_file()
    ):
        raise ValueError("existing persistent SQLite required")
    directory = Path(url.database).parent
    with file_lock(directory / "worker.lock") as acquired:
        if not acquired:
            print(json.dumps({"status": "busy"}, ensure_ascii=False))
            return
        engine = create_engine(url)
        verify_sqlite_schema(engine)
        try:
            result = asyncio.run(
                run_cycle(engine, owner_user_id=owner_user_id, owner_chat_id=owner_chat_id)
            )
        finally:
            engine.dispose()
    print(json.dumps(result, ensure_ascii=False))


def run_cli() -> None:
    """Expose a safe failure category to intake without logging exception values."""
    try:
        main()
    except Exception as error:
        frames = traceback.extract_tb(error.__traceback__)
        last = frames[-1]
        print(json.dumps({
            "status": "failed",
            "error_type": type(error).__name__,
            "failure_location": {
                "module": Path(last.filename).name,
                "function": last.name,
                "line": last.lineno,
            },
        }, ensure_ascii=False))
        raise SystemExit(1) from None


if __name__ == "__main__":
    run_cli()
