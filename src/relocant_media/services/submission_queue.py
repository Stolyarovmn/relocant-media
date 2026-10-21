"""Private submissions influence review order, never become publication evidence."""
import re
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from relocant_media.domain.dedup import EventCandidate, same_event
from relocant_media.storage.models import EditorialRun, RawItem, Source, Submission

NEWS_KINDS = ("correction", "news_tip", "event", "question")


def batch_submissions(session: Session, *, limit: int = 20) -> list[Submission]:
    rows = session.scalars(select(Submission).where(Submission.kind.in_(NEWS_KINDS),
        Submission.status.in_(("new", "source_linked")),
        Submission.payload["city"].as_string() == "belgrade",
        Submission.payload["test_only"].as_boolean().is_not(True),
    ).order_by(case((Submission.status == "new", 0), else_=1), Submission.id))
    reviewed_rows = list(session.scalars(select(RawItem).join(EditorialRun).join(Source).where(
        Source.city == "belgrade")))
    reviewed = {row.id for row in reviewed_rows}
    def candidate(row: RawItem) -> EventCandidate:
        return EventCandidate(raw_item_id=row.id,source_id=row.source_id,url=row.url,
            title=row.title,body=row.body,published_at=row.published_at,
            canonical_url=row.canonical_url,content_sha256=row.content_sha256)
    reviewed_events = [candidate(row) for row in reviewed_rows]
    def needs_review(raw_id: int) -> bool:
        if raw_id in reviewed:
            return False
        raw = session.get(RawItem, raw_id)
        return raw is not None and not any(same_event(candidate(raw), old)
                                          for old in reviewed_events)
    result = []
    for row in rows:
        ids = row.payload.get("raw_item_ids", [])
        if row.status == "new" or any(needs_review(raw_id) for raw_id in ids):
            result.append(row)
            if len(result) >= limit:
                break
    return result


def pending_submissions(session: Session, *, limit: int = 20) -> list[Submission]:
    rows = list(session.scalars(select(Submission).where(
        Submission.kind.in_(NEWS_KINDS), Submission.status.in_(("new", "waiting_primary")),
        Submission.payload["city"].as_string() == "belgrade",
    ).order_by(case((Submission.status == "new", 0), else_=1),
               case((Submission.kind == "correction", 0), else_=1),
               Submission.payload["last_triaged_at"].as_string().asc().nullsfirst(),
               Submission.created_at, Submission.id).limit(limit)))
    return rows


def tip_urls(payload: dict[str, Any]) -> list[str]:
    # Local extraction only. No private text is used as a public search query or evidence.
    text = payload.get("text", "")
    if not isinstance(text, str):
        return []
    return list(dict.fromkeys(url.rstrip(').,;!?]}»') for url in
        re.findall(r"https://[^\s<>\"']+", text)))[:5]


def priority_raw_ids(session: Session) -> set[int]:
    rows = session.scalars(select(Submission).where(
        Submission.kind.in_(NEWS_KINDS), Submission.status == "source_linked"))
    return {raw_id for row in rows if row.payload.get("city") == "belgrade"
            for raw_id in row.payload.get("raw_item_ids", []) if type(raw_id) is int}
