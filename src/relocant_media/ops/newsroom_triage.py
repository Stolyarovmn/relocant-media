"""Deterministic queue hygiene; no model calls and no duplicate article bodies."""
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.domain.freshness import freshness_decision
from relocant_media.domain.policy import CURRENT_EDITORIAL_RULE_VERSION
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import EditorialRun, RawItem, Source


def triage_queue(engine: Engine, *, now: datetime) -> dict[str, Any]:
    decisions: list[dict[str, Any]] = []
    with Session(engine) as session:
        lock_editorial_city(session, "belgrade")
        rows = session.execute(select(RawItem).join(Source).where(
            Source.city == "belgrade", Source.enabled.is_(True),
        ).order_by(RawItem.id)).scalars()
        for item in rows:
            run = session.get(EditorialRun, item.id)
            # Completed reviews, drafts and delivery claims remain immutable.
            if run is not None and (run.post_id is not None
                    or run.delivery_status != "pending"
                    or run.outcome.get("reason") != "source_auto_publish_disabled"
                    or run.staging_message_id is not None
                    or any(run.outcome.get(k) is not None
                           for k in ("claims", "draft", "verification"))
                    or "_package" in run.outcome or "_autonomous_assessment" in run.outcome):
                continue
            result = freshness_decision(title=item.title, body=item.body,
                published_at=item.published_at, now=now)
            if result.action == "keep":
                continue
            status = "skipped" if result.action == "discard" else "human_review"
            outcome = {"status": status, "reason": result.reason,
                "triage_version": "freshness-v1", "ai_calls": 0,
                "checked_at": now.astimezone(UTC).isoformat(),
                "autonomous_resolution": "rejected" if status == "skipped" else "deferred_metadata"}
            if run is None:
                run = EditorialRun(raw_item_id=item.id, outcome=outcome,
                    evidence={"raw_item_id": item.id, "source_id": item.source_id,
                              "content_sha256": item.content_sha256},
                    editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
                    delivery_status="pending")
                session.add(run)
            else:
                run.outcome = {**outcome, "previous_decision": run.outcome}
            decisions.append({"raw_item_id": item.id, "status": status,
                              "reason": result.reason, "ai_calls": 0})
        session.commit()
    return {"version": 1, "ai_calls": 0, "decisions": decisions,
            "discarded": sum(d["status"] == "skipped" for d in decisions),
            "metadata_deferred": sum(d["status"] == "human_review" for d in decisions)}
