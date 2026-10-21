"""Private submissions influence review order, never become publication evidence."""
import re
from typing import Any

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from relocant_media.storage.models import Submission

NEWS_KINDS = ("correction", "news_tip", "event", "question")


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
