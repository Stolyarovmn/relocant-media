"""Post-run checks against actual delivery receipts; no model calls or sends."""

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.domain.draft_language import draft_language_reason
from relocant_media.domain.freshness import draft_date_reason
from relocant_media.storage.models import EditorialRun, Post


def audit_cycle(session: Session, report: dict[str, Any]) -> dict[str, Any]:
    pins = report.get("pin_maintenance", {})
    intake = report.get("private_intake", {})
    checks = dict(
        primary_completed=report.get("status") in {"PRIMARY_OK", "PRIMARY_PARTIAL"},
        no_processing_errors=not report.get("processing_errors"),
        no_fallback_needed=report.get("fallback_required") is False,
        pin_maintenance_verified=report.get("channel_transport") == "disabled"
        or (bool(pins) and not pins.get("blocked") and pins.get("owner_action_required") is False),
        unique_receipts=True,
        receipts_match_database=True,
        russian_copy=True,
        title_prefix=True,
        absolute_dates=True,
        private_intake_healthy=intake.get("status") in (None, "disabled", "ready"),
        private_feedback_healthy=report.get("private_feedback", {}).get("status")
            in (None, "disabled", "ready"),
    )
    ids = report.get("sent_message_ids", [])
    checks["unique_receipts"] = len(ids) == len(set(ids))
    messages = []
    for mid in ids:
        if type(mid) is not int or mid <= 0:
            checks["receipts_match_database"] = False
            continue
        rows = session.execute(
            select(Post, EditorialRun)
            .join(EditorialRun, EditorialRun.post_id == Post.id)
            .where(Post.telegram_message_id == mid, Post.city == "belgrade")
        ).all()
        valid = len(rows) == 1 and rows[0][1].delivery_status == "sent"
        checks["receipts_match_database"] &= valid
        if not valid:
            continue
        post, run = rows[0]
        language_reason = draft_language_reason(post.title, post.body)
        checks["russian_copy"] &= language_reason is None
        checks["title_prefix"] &= post.title.startswith("[тестовый запуск]")
        checks["absolute_dates"] &= draft_date_reason(post.title,post.body) is None
        messages.append(
            dict(
                message_id=mid,
                post_id=post.id,
                raw_item_id=run.raw_item_id,
                title=post.title,
                language_reason=language_reason,
                category=post.category,
                editorial_rule_version=post.editorial_rule_version,
            )
        )
    passed = all(checks.values())
    status = ("passed" if ids else "passed_no_publications") if passed else "requires_review"
    return dict(
        status=status,
        checks=checks,
        messages=messages,
        ai_calls=0,
        telegram_sends=0,
        scope="Проверены сохранённые тексты и квитанции; вид Telegram сверяется отдельно.",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args()
    if not args.database.is_file():
        raise ValueError("existing database required")
    report = json.loads((args.run / "server-report.json").read_text(encoding="utf-8"))
    engine = create_engine(f"sqlite:///file:{args.database.resolve().as_posix()}?mode=ro&uri=true")
    try:
        with Session(engine) as session:
            result = audit_cycle(session, report)
    finally:
        engine.dispose()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
