"""Match private news tips against public material without exporting reader data."""

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from relocant_media.domain.event_facts import (
    event_dates,
    location_anchors,
    material_words,
    numeric_facts,
)
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.services.primary_discovery import folded, topic_words
from relocant_media.services.reader_publication import POLICY, material_fingerprint
from relocant_media.storage.models import RawItem, Submission

DISCOVERY_FIELD = "reader_source_discovery"
SOURCE_REVISION_FIELD = "reader_source_revision_sha256"
RECHECK_INTERVAL = timedelta(hours=6)
CHECK_WINDOW = timedelta(hours=48)
REVIEW_CANDIDATE_LIMIT = 3
_QUOTED_NAME = re.compile(r'[«„“"]([^»“”"\n]{5,100})[»“”"]')
_NAMED_VENUE = re.compile(
    r"(?im)\b(?:площадка|место|venue|lokacija|mesto)\s*:\s*([^\r\n;.!]{5,100})",
)
_RELATIVE_DAY = re.compile(r"(?iu)\b(?:сегодня|вчера|завтра|danas|juče|juce|sutra)\b")


def submission_time(row: Submission) -> datetime:
    stamp = row.created_at
    accepted = row.payload.get("accepted_at")
    if isinstance(accepted, str):
        try:
            stamp = datetime.fromisoformat(accepted)
        except ValueError:
            pass
    return stamp.replace(tzinfo=stamp.tzinfo or UTC)


def _dates_in(value: str, year: int) -> frozenset[str]:
    return event_dates(value, year) | event_dates(folded(value), year)


def _confirmed_source_choice(payload: dict[str, Any]) -> dict[str, Any] | None:
    publication = payload.get("reader_publication")
    if (
        isinstance(publication, dict)
        and publication.get("version") == POLICY["version"]
        and publication.get("stage") == "confirmed"
        and publication.get("ownership") == "source"
        and publication.get("permission") == "private"
    ):
        return publication
    return None


def confirmed_source_signal(payload: dict[str, Any]) -> bool:
    publication = _confirmed_source_choice(payload)
    if publication is None:
        return False
    return material_fingerprint(payload) in (
        publication.get("material_sha256"), payload.get(SOURCE_REVISION_FIELD),
    )


def record_private_source_revision(row: Submission) -> None:
    """Keep author follow-ups searchable within the same private source request."""
    if row.kind != "news_tip" or row.status != "reader_review":
        return
    publication = _confirmed_source_choice(row.payload)
    if publication is None:
        return
    revision = material_fingerprint(row.payload)
    if revision == row.payload.get(SOURCE_REVISION_FIELD):
        return
    if revision == publication.get("material_sha256") and SOURCE_REVISION_FIELD not in row.payload:
        return
    payload = {**row.payload, SOURCE_REVISION_FIELD: revision}
    payload.pop(DISCOVERY_FIELD, None)
    row.payload = payload


def pending_source_discovery(
    session: Session, *, city: str, now: datetime,
    has_article_link: Callable[[dict[str, Any]], bool],
) -> list[Submission]:
    checked = Submission.payload[DISCOVERY_FIELD]["checked_at"].as_string()
    lookup_status = Submission.payload[DISCOVERY_FIELD]["status"].as_string()
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
        or_(checked.is_(None), checked <= (now - RECHECK_INTERVAL).isoformat(),
            lookup_status == "forward_lookup_deferred"),
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
    dates = _dates_in(first_line, year) or _dates_in(signal, year)
    excerpt = public_body.strip().partition("\n\n")[0][:650]
    source_dates = _dates_in(public_title, source_year) or _dates_in(excerpt, source_year)
    if len(dates) != 1 or dates != source_dates:
        return False
    left_location = {folded(place) for place in location_anchors(signal)}
    right_location = {folded(place) for place in location_anchors(f"{public_title}\n{excerpt}")}
    return not (left_location and right_location and left_location.isdisjoint(right_location))


def _named_values(pattern: re.Pattern[str], text: str) -> set[str]:
    return {
        " ".join(re.findall(r"[a-z0-9]+", folded(value)))
        for value in pattern.findall(text) if len(topic_words(value)) >= 2
    }


def undated_reader_match(
    text: str, title: str, body: str, *, submitted_at: datetime, published_at: datetime,
) -> Literal["anchored", "review"] | None:
    """Undated overlap is a lead; a shared named event and venue can identify it."""
    published_at = published_at.replace(tzinfo=published_at.tzinfo or UTC)
    if abs(published_at - submitted_at) > CHECK_WINDOW:
        return None
    signal = text[:4000].strip()
    if _dates_in(signal, submitted_at.year):
        return None
    opening = body.strip().partition("\n\n")[0][:650]
    source = f"{title[:250]}\n{opening}"
    if len(topic_words(signal[:900]) & topic_words(source)) < 4:
        return None
    left_locations = {folded(place) for place in location_anchors(signal)}
    right_locations = {folded(place) for place in location_anchors(source)}
    left_venues = _named_values(_NAMED_VENUE, signal)
    right_venues = _named_values(_NAMED_VENUE, source)
    if (
        (left_venues and right_venues and left_venues.isdisjoint(right_venues))
        or (not left_venues and not right_venues and left_locations and right_locations
            and left_locations.isdisjoint(right_locations))
    ):
        return None
    left_facts = numeric_facts(signal, submitted_at.year)
    right_dates = _dates_in(title, published_at.year) or _dates_in(opening, published_at.year)
    right_facts = numeric_facts(source, published_at.year) - _dates_in(source, published_at.year)
    if left_facts and right_facts and left_facts.isdisjoint(right_facts):
        return None
    if (
        left_venues & right_venues
        and _named_values(_QUOTED_NAME, signal) & _named_values(_QUOTED_NAME, source)
        and len(right_dates) == 1
        and not _RELATIVE_DAY.search(signal)
        and left_facts <= right_facts
        and material_words(signal) == material_words(source)
    ):
        return "anchored"
    return "review"


def review_candidates(row: Submission) -> tuple[dict[str, Any], ...]:
    check = row.payload.get(DISCOVERY_FIELD, {})
    if (
        row.kind != "news_tip" or row.status != "reader_review"
        or not confirmed_source_signal(row.payload)
        or not isinstance(check, dict) or check.get("status") != "candidate_review"
        or check.get("material_sha256") != material_fingerprint(row.payload)
    ):
        return ()
    items = check.get("candidates", ())
    if not isinstance(items, list):
        return ()
    return tuple(
        item for item in items[:REVIEW_CANDIDATE_LIMIT] if isinstance(item, dict)
        and type(item.get("id")) is int and item["id"] > 0
        and isinstance(item.get("title"), str) and isinstance(item.get("sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
        and isinstance(item.get("url"), str) and len(item["url"]) <= 400
        and is_public_https_url(item["url"])
    )


def record_discovery_check(
    row: Submission, *, now: datetime, status: str, candidates: tuple[RawItem, ...] = (),
    remaining_forwards: tuple[str, ...] = (),
) -> None:
    check: dict[str, Any] = {
        "checked_at": now.isoformat(), "status": status,
        "material_sha256": material_fingerprint(row.payload),
    }
    if candidates:
        check["candidates"] = [
            {"id": item.id, "title": item.title[:160], "url": item.url,
             "sha256": item.content_sha256}
            for item in candidates[:REVIEW_CANDIDATE_LIMIT]
            if item.content_sha256 and is_public_https_url(item.url) and len(item.url) <= 400
        ]
    if status == "candidate_review" and not check.get("candidates"):
        check["status"] = "ambiguous"
    if remaining_forwards:
        check["remaining_forwards"] = list(remaining_forwards)
    row.payload = {**row.payload, DISCOVERY_FIELD: check, "updated_at": now.isoformat()}


def discovery_deadline_reached(row: Submission, now: datetime) -> bool:
    check = row.payload.get(DISCOVERY_FIELD, {})
    if not isinstance(check, dict) or check.get("status") not in {"no_match", "ambiguous"}:
        return False
    if not confirmed_source_signal(row.payload):
        return False
    if check.get("material_sha256") != material_fingerprint(row.payload):
        return False
    return now - submission_time(row) >= CHECK_WINDOW
