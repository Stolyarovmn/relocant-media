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
    NEW_CASE_LABEL,
    OPEN_STATES,
    ROUTES,
    WELCOME_MENU,
)
from relocant_media.services.submission_reference import submission_reference
from relocant_media.storage.models import Submission, TelegramConversation, TelegramUpdateReceipt


def export_intake_state(session: Session, at: datetime) -> dict[str, Any]:
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
    return {
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


def form_settings() -> dict[str, Any]:
    return {"menu": WELCOME_MENU, "new_case": NEW_CASE_LABEL, "routes": ROUTES}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path, help="verified SQLite snapshot")
    parser.add_argument("output", type=Path, help="new private file, outside the repository")
    args = parser.parse_args()
    database = args.database.resolve(strict=True)
    engine = create_engine(
        "sqlite://", creator=lambda: sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True),
    )
    try:
        with Session(engine) as session:
            state = export_intake_state(session, datetime.now(UTC))
        with os.fdopen(
            os.open(args.output, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600),
            "w", encoding="utf-8",
        ) as stream:
            json.dump(state, stream, ensure_ascii=False)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
