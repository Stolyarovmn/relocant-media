"""Turn a reader's own permitted material into an attributed reader-report document.

The reader text becomes evidence only for the statement «reader reports X»: the draft must
attribute it, low-risk categories alone may auto-publish, and all ordinary editorial checks,
two independent reviews and delivery guards still run. No profile or chat data is copied.
"""

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.domain.policy import READER_REPORT_CATEGORIES, READER_REPORT_REVIEW_REASON
from relocant_media.domain.topic_tags import is_topic_footer
from relocant_media.services.reader_publication import (
    POLICY,
    material_fingerprint,
    permission_reason,
    reader_credit,
)
from relocant_media.storage.models import RawItem, Source, Submission

READER_REPORT_SOURCE_TYPE = "reader_report"
READER_REPORT_OWNERSHIP = "reader"
READER_REPORT_FIELD = "reader_report"
__all__ = ["READER_REPORT_CATEGORIES", "READER_REPORT_REVIEW_REASON"]
MIN_TEXT_CHARS = 60
MAX_BODY_CHARS = 6000
MAX_AGE = timedelta(days=7)
BATCH_LIMIT = 5
_URL = re.compile(r"https?://\S+")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)")
_ATTRIBUTION = re.compile(r"читател", re.IGNORECASE)
HEADER = "Сообщение читателя, присланное в предложку канала. Редакция его не подтверждала."


def reader_source_id(city: str) -> str:
    return f"reader_reports_{city}"


def is_reader_report_source(source: Source) -> bool:
    return (source.source_type == READER_REPORT_SOURCE_TYPE
            and source.ownership == READER_REPORT_OWNERSHIP)


def is_reader_report_document(source_id: str) -> bool:
    return source_id.startswith("reader_reports_")


def ensure_reader_source(session: Session, city: str) -> Source:
    source = session.get(Source, reader_source_id(city))
    values = dict(city=city, name="Сообщения читателей", url=POLICY["contact_url"],
                  source_type=READER_REPORT_SOURCE_TYPE, trust=Decimal("0.500"),
                  ownership=READER_REPORT_OWNERSHIP, high_risk=False, auto_publish=True,
                  enabled=True)
    if source is None:
        source = Source(id=reader_source_id(city), **values)
        session.add(source)
    else:
        for key, value in values.items():
            setattr(source, key, value)
    session.flush()
    return source


def _content(text: str) -> str:
    return _URL.sub("", text).strip()


def ineligible_reason(row: Submission, *, now: datetime) -> str | None:
    """General gate; any reason leaves the material in the owner's private card."""
    payload = row.payload
    if (row.kind != "news_tip" or row.status != "reader_review"
            or payload.get("reader_intake") is not True or payload.get("test_only")):
        return "not_reader_material"
    if permission_reason(payload, POLICY["channel_id"]) is not None:
        return "no_publication_permission"
    text = payload.get("text")
    if not isinstance(text, str) or len(_content(text)) < MIN_TEXT_CHARS:
        return "text_too_short"
    if len(text) > MAX_BODY_CHARS:
        return "text_too_long"
    if _EMAIL.search(text) or _PHONE.search(_URL.sub("", text)):
        return "contact_details_present"
    accepted = payload.get("accepted_at")
    try:
        accepted_at = datetime.fromisoformat(accepted) if isinstance(accepted, str) else None
    except ValueError:
        accepted_at = None
    if accepted_at is None:
        return "acceptance_time_missing"
    if now - accepted_at.replace(tzinfo=accepted_at.tzinfo or UTC) > MAX_AGE:
        return "submission_too_old"
    return None


def report_body(payload: dict[str, Any]) -> str:
    credit = reader_credit(payload)
    lines = [HEADER, "", payload["text"].strip()]
    if credit:
        lines += ["", f"Подпись, выбранная автором для публикации: {credit}"]
    return "\n".join(lines)


def report_title(text: str) -> str:
    first = next((line.strip() for line in text.splitlines() if _content(line)), "")
    first = _content(first) or "Сообщение читателя"
    return first if len(first) <= 160 else first[:157].rstrip() + "…"


def materialize_reader_reports(
    session: Session, *, city: str, now: datetime, limit: int = BATCH_LIMIT,
) -> list[int]:
    """Idempotent per material fingerprint; caller commits."""
    rows = session.scalars(select(Submission).where(
        Submission.kind == "news_tip", Submission.status == "reader_review",
        Submission.payload["city"].as_string() == city,
    ).order_by(Submission.id))
    created: list[int] = []
    source: Source | None = None
    for row in rows:
        if len(created) >= limit:
            break
        if ineligible_reason(row, now=now) is not None:
            continue
        sha = material_fingerprint(row.payload)
        external_id = f"submission-{row.id}-{sha[:12]}"
        source = source or ensure_reader_source(session, city)
        raw = session.scalar(select(RawItem).where(
            RawItem.source_id == source.id, RawItem.external_id == external_id))
        if raw is None:
            text = row.payload["text"]
            raw = RawItem(
                source_id=source.id, external_id=external_id,
                url=f"{POLICY['contact_url']}?start=report-{row.id}",
                title=report_title(text), body=report_body(row.payload),
                published_at=now, published_at_precision="instant",
            )
            session.add(raw)
            session.flush()
        row.payload = {
            **row.payload, "raw_item_ids": [raw.id],
            READER_REPORT_FIELD: {"raw_item_id": raw.id, "material_sha256": sha,
                                  "at": now.isoformat()},
            "triage_reason": "reader_report", "last_triaged_at": now.isoformat(),
        }
        row.status = "source_linked"
        created.append(raw.id)
    return created


def submission_id_for(raw: RawItem) -> int | None:
    match = re.fullmatch(r"submission-([1-9][0-9]{0,17})-[0-9a-f]{12}", raw.external_id)
    return int(match.group(1)) if match else None


def reader_report_delivery_reason(
    submission: Submission, raw: RawItem, *, title: str, body: str,
) -> str | None:
    """Exact link between the published draft, the document and the reader's material."""
    record = submission.payload.get(READER_REPORT_FIELD)
    sha = material_fingerprint(submission.payload)
    if (not isinstance(record, dict) or record.get("raw_item_id") != raw.id
            or record.get("material_sha256") != sha
            or raw.external_id != f"submission-{submission.id}-{sha[:12]}"
            or raw.body != report_body(submission.payload)):
        return "reader report no longer matches the reader material"
    text = f"{title}\n{body}"
    if not _ATTRIBUTION.search(text):
        return "reader report must be attributed to the reader"
    if _EMAIL.search(text) or _PHONE.search(_URL.sub("", text)):
        return "reader report must not contain contact details"
    return None


def with_reader_credit(body: str, credit: str) -> str:
    """Put the reader-chosen credit on its own line before the topic footer, before reviews."""
    if not credit or credit in body:
        return body
    main, separator, last = body.rpartition("\n\n")
    if separator and is_topic_footer(last.strip()):
        return f"{main}\n\n{credit}\n\n{last}"
    return f"{body}\n\n{credit}"
