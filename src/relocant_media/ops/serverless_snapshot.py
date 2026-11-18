"""Read the reader and Telegram state needed to rehearse a Serverless transition."""

import argparse
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID
from relocant_media.services.reader_intake import (
    DRAFT_STATES,
    NEW_CASE_LABEL,
    OPEN_STATES,
    ROUTES,
    WELCOME_MENU,
)
from relocant_media.services.submission_reference import submission_reference
from relocant_media.storage.models import Submission, TelegramConversation, TelegramUpdateReceipt


def open_draft_routes(database: Path) -> list[dict[str, Any]]:
    """Only minimal routing identities from a verified immutable canonical snapshot."""
    engine = create_engine(
        "sqlite://", creator=lambda: sqlite3.connect(
            f"{database.resolve(strict=True).as_uri()}?mode=ro", uri=True,
        ),
    )
    try:
        with Session(engine) as session:
            routes = []
            for row in session.scalars(select(Submission).where(
                Submission.payload["reader_intake"].as_boolean().is_(True),
                Submission.status.in_(DRAFT_STATES),
            ).order_by(Submission.id)):
                chat_id, topic_id = row.payload.get("chat_id"), row.payload.get("topic_id")
                if (type(row.telegram_user_id) is not int or row.telegram_user_id <= 0
                        or type(chat_id) is not int
                        or (topic_id is not None and (type(topic_id) is not int or topic_id <= 0))):
                    raise ValueError("invalid canonical reader routing identity")
                routes.append({"reference": submission_reference(row),
                               "telegram_user_id": row.telegram_user_id,
                               "chat_id": chat_id, "topic_id": topic_id})
            return routes
    finally:
        engine.dispose()


def export_intake_state(
    session: Session, at: datetime, *, bot_id: int | None = None,
) -> dict[str, Any]:
    if bot_id is not None and (type(bot_id) is not int or bot_id <= 0):
        raise ValueError("verified bot identifier required")
    cases = session.scalars(
        select(Submission)
        .where(
            Submission.payload["reader_intake"].as_boolean().is_(True),
            Submission.status.in_(OPEN_STATES),
        )
        .order_by(Submission.id)
    )
    receipts = session.scalars(
        select(TelegramUpdateReceipt.update_id)
        .where(TelegramUpdateReceipt.received_at >= at - timedelta(hours=25))
        .order_by(TelegramUpdateReceipt.update_id)
    ).all()
    unresolved = session.scalar(
        select(TelegramUpdateReceipt.update_id)
        .where(TelegramUpdateReceipt.status.in_(("processing", "failed")))
        .limit(1)
    )
    if unresolved is not None:
        raise ValueError("the snapshot contains unresolved Telegram updates")
    result = {
        "version": 1,
        "created_at": at.isoformat(),
        "channel_id": APPROVED_CHANNEL_ID,
        "form": form_settings(),
        "processed_updates": receipts,
        "legacy_conversations": list(session.scalars(
            select(TelegramConversation.telegram_user_id),
        )),
        "cases": [
            {
                "id": row.id,
                "reference": submission_reference(row),
                "kind": row.kind,
                "status": row.status,
                "telegram_user_id": row.telegram_user_id,
                "telegram_message_id": row.telegram_message_id,
                "payload": row.payload,
            }
            for row in cases
        ],
    }
    if bot_id is not None:
        result["bot_id"] = bot_id
    return result


def form_settings() -> dict[str, Any]:
    return {"menu": WELCOME_MENU, "new_case": NEW_CASE_LABEL, "routes": ROUTES}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="verified SQLite snapshot")
    parser.add_argument("output", type=Path, help="new private file, outside the repository")
    parser.add_argument("--bot-id", type=int, help="verified getMe identifier")
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    engine = create_engine(
        "sqlite://", creator=lambda: sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True),
    )
    try:
        with Session(engine) as session:
            state = export_intake_state(session, datetime.now(UTC), bot_id=args.bot_id)
        with os.fdopen(
            os.open(args.output, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600),
            "w", encoding="utf-8",
        ) as stream:
            json.dump(state, stream, ensure_ascii=False)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
