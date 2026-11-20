"""Match private news tips against public material without exporting reader data."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from relocant_media.domain.event_facts import event_dates, location_anchors
from relocant_media.services.primary_discovery import folded, topic_words
from relocant_media.services.reader_publication import POLICY, material_fingerprint
from relocant_media.storage.models import Submission

DISCOVERY_FIELD = "reader_source_discovery"
RECHECK_INTERVAL = timedelta(hours=6)
CHECK_WINDOW = timedelta(hours=48)


def confirmed_source_signal(payload: dict[str, Any]) -> bool:
    publication = payload.get("reader_publication")
    return bool(
        isinstance(publication, dict)
        and publication.get("version") == POLICY["version"]
        and publication.get("stage") == "confirmed"
        and publication.get("ownership") == "source"
        and publication.get("permission") == "private"
        and publication.get("material_sha256") == material_fingerprint(payload)
    )


def pending_source_discovery(
    session: Session, *, city: str, now: datetime,
    has_article_link: Callable[[dict[str, Any]], bool],
) -> list[Submission]:
    checked = Submission.payload[DISCOVERY_FIELD]["checked_at"].as_string()
    publication = Submission.payload["reader_publication"]
    rows = session.scalars(select(Submission).where(
        Submission.kind == "news_tip",
        Submission.status == "reader_review",
        Submission.payload["city"].as_string() == city,
        Submission.payload["reader_intake"].as_boolean().is_(True),
        Submission.payload["test_only"].as_boolean().is_not(True),
        publication["ownership"].as_string() == "source",
        publication["permission"].as_string() == "private",
        publication["stage"].as_string() == "confirmed",
        or_(checked.is_(None), checked <= (now - RECHECK_INTERVAL).isoformat()),
    ).order_by(checked.asc().nullsfirst(), Submission.id))
    eligible = []
    for row in rows:
        if confirmed_source_signal(row.payload) and not has_article_link(row.payload):
            eligible.append(row)
            if len(eligible) == 12:
                break
    return eligible


def matches_reader_event(
    text: str, public_title: str, public_body: str, *, year: int, source_year: int,
) -> bool:
    """A local candidate match needs a distinct title and one common event date."""
    signal = text[:4000].strip()
    first_line = signal.split("\n", 1)[0][:250]
    if len(topic_words(first_line) & topic_words(public_title[:250])) < 3:
        return False
    # Prefer the date in the headline or opening paragraph to ticket-sale dates later on.
    def dates_in(value: str, default_year: int) -> frozenset[str]:
        return event_dates(value, default_year) | event_dates(folded(value), default_year)

    dates = dates_in(first_line, year) or dates_in(signal, year)
    excerpt = public_body.strip().partition("\n\n")[0][:650]
    source_dates = dates_in(public_title, source_year) or dates_in(excerpt, source_year)
    if len(dates) != 1 or dates != source_dates:
        return False
    left_location = {folded(place) for place in location_anchors(signal)}
    right_location = {folded(place) for place in location_anchors(f"{public_title}\n{excerpt}")}
    return not (left_location and right_location and left_location.isdisjoint(right_location))


def record_discovery_check(row: Submission, *, now: datetime, status: str) -> None:
    row.payload = {**row.payload, DISCOVERY_FIELD: {
        "checked_at": now.isoformat(), "status": status,
        "material_sha256": material_fingerprint(row.payload),
    }, "updated_at": now.isoformat()}


def discovery_deadline_reached(row: Submission, now: datetime) -> bool:
    check = row.payload.get(DISCOVERY_FIELD, {})
    if check.get("status") not in {"no_match", "ambiguous"}:
        return False
    if not confirmed_source_signal(row.payload):
        return False
    if check.get("material_sha256") != material_fingerprint(row.payload):
        return False
    accepted = row.payload.get("accepted_at")
    started = row.created_at
    if isinstance(accepted, str):
        try:
            started = datetime.fromisoformat(accepted)
        except ValueError:
            pass
    return now - started.replace(tzinfo=started.tzinfo or UTC) >= CHECK_WINDOW
