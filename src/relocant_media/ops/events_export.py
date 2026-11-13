"""Отдельная публичная афиша: только опубликованные, проверенные место и время."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine, select
from sqlalchemy.engine import ScalarResult
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.package import (
    EditorialPackage,
    canonical_json,
    context_sha256,
    draft_sha256,
)
from relocant_media.ai.pipeline import _citations_are_grounded
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.map_links import map_event_id
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.ops.newsroom_export import public_article_url
from relocant_media.services.event_place import resolve_saved_event_place
from relocant_media.services.event_place_adapters import cukarica_stage_start
from relocant_media.services.pin_policy import active_map_ad
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source, Submission

MAX_EVENTS = 1000
MAX_FEED_BYTES = 2_000_000
# Другой город включается явно, вместе с проверенным публичным каналом.
PUBLIC_CHANNELS = {"belgrade": "eduvbelgrad"}
Label = Annotated[str, Field(min_length=1, max_length=500)]


class PublicModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Venue(PublicModel):
    name: Label
    address: Label
    latitude: float = Field(ge=-90, le=90, allow_inf_nan=False)
    longitude: float = Field(ge=-180, le=180, allow_inf_nan=False)


class PublicEvent(PublicModel):
    id: str = Field(pattern=r"^event_[0-9a-f]{20}$")
    kind: Literal["event"] = "event"
    title: Label
    source_url: str
    post_url: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    expires_at: AwareDatetime
    timezone: str
    archived: bool
    venue: Venue
    coordinate_source_url: str
    venue_source_url: str
    schedule_source_url: str | None = None
    sponsored: bool = False

    @field_validator("source_url", "coordinate_source_url", "venue_source_url")
    @classmethod
    def public_url(cls, value: str) -> str:
        if not is_public_https_url(value) or len(value) > 2048:
            raise ValueError("public HTTPS source required")
        return value

    @field_validator("schedule_source_url")
    @classmethod
    def optional_public_url(cls, value: str | None) -> str | None:
        return cls.public_url(value) if value is not None else None

    @field_validator("post_url")
    @classmethod
    def published_url(cls, value: str) -> str:
        channels = "|".join(re.escape(channel) for channel in PUBLIC_CHANNELS.values())
        if not re.fullmatch(rf"https://t\.me/(?:{channels})/[1-9]\d*", value):
            raise ValueError("public channel post required")
        return value

    @model_validator(mode="after")
    def consistent(self) -> PublicEvent:
        zone = ZoneInfo(self.timezone)
        if self.id != map_event_id(self.source_url):
            raise ValueError("event identifier does not match source")
        if self.ends_at is not None:
            if self.ends_at <= self.starts_at or self.expires_at != self.ends_at:
                raise ValueError("invalid event end")
        elif self.expires_at != datetime.combine(
            self.starts_at.astimezone(zone).date() + timedelta(days=1),
            time.min,
            zone,
        ):
            raise ValueError("unknown end requires the next local midnight cutoff")
        return self


class EventsFeed(PublicModel):
    version: Literal[1] = 1
    generated_at: AwareDatetime | None = None
    events: tuple[PublicEvent, ...] = Field(default=(), max_length=MAX_EVENTS)

    @model_validator(mode="after")
    def unique_events(self) -> EventsFeed:
        if len({event.id for event in self.events}) != len(self.events):
            raise ValueError("duplicate event identifier")
        if self.events and self.generated_at is None:
            raise ValueError("event feed timestamp required")
        if self.generated_at is not None and any(
            event.archived != (event.expires_at <= self.generated_at) for event in self.events
        ):
            raise ValueError("event archive state differs from snapshot time")
        return self


def read_events_feed(content: bytes) -> EventsFeed:
    if len(content) > MAX_FEED_BYTES:
        raise ValueError("event feed too large")
    return EventsFeed.model_validate_json(content)


def write_events_feed(path: Path, feed: EventsFeed) -> None:
    content = feed.model_dump_json().encode("utf-8")
    read_events_feed(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)


def _reviewed_package(
    session: Session, run: EditorialRun, post: Post, item: RawItem
) -> EditorialPackage | None:
    """Проверить историческую аттестацию, не применять к ней текущую давность ревью."""
    payload = run.outcome.get("_package")
    if not isinstance(payload, dict) or post.published_at is None:
        return None
    if hashlib.sha256(canonical_json(payload).encode()).hexdigest() != run.outcome.get(
        "_package_sha256"
    ):
        return None
    package = EditorialPackage.model_validate_json(json.dumps(payload))
    if (
        package.raw_item_id != item.id
        or package.source_id != item.source_id
        or package.article_id != item.external_id
        or package.city != post.city
        or package.outcome.ranking.category != "events"
        or (post.title, post.body) != (package.outcome.draft.title, package.outcome.draft.body)
    ):
        return None
    if package.outcome.status != "ready":
        if package.outcome.status != "human_review":
            return None
        receipts: ScalarResult[dict[str, Any]] = session.scalars(
            select(Submission.payload)
            .where(
                Submission.kind == "owner_editorial_review",
                Submission.status == "published",
                Submission.payload["raw_item_id"].as_integer() == item.id,
            )
            .limit(5)
        )
        if not any(
            receipt.get("package_sha256") == run.outcome.get("_package_sha256")
            and receipt.get("draft_sha256") == draft_sha256(package.outcome.draft)
            and receipt.get("telegram_channel_message_id") == post.telegram_message_id
            and isinstance(receipt.get("published_at"), str)
            and abs(datetime.fromisoformat(receipt["published_at"]) - post.published_at)
            < timedelta(seconds=5)
            for receipt in receipts
        ):
            return None
    reviews = package.reviews
    if {review.role for review in reviews} != {"facts", "utility"} or len(
        {r.reviewer_id for r in reviews}
    ) != 2:
        return None
    for review in reviews:
        if (
            review.verdict != "approve"
            or review.score < 75
            or review.flags
            or not review.live_source_checked
            or review.context_sha256 != context_sha256(package)
            or review.draft_sha256 != draft_sha256(package.outcome.draft)
            or review.source_id != package.source_id
            or review.article_id != package.article_id
            or review.reviewed_at > post.published_at
            or post.published_at - review.reviewed_at > timedelta(hours=2 if package.urgent else 24)
        ):
            return None
    documents = tuple(package.event.documents) if package.event else (package.evidence,)
    if not _citations_are_grounded(package.outcome.claims, documents):
        return None
    return package


def _backfill_claimed(
    package: EditorialPackage, document: EvidenceDocument, place: dict[str, Any]
) -> bool:
    """Старое ревью охватывает площадку, день и часы; год всегда берётся из точного источника."""
    outcome = package.outcome
    supported = {
        v.claim_id for v in outcome.verification.claims if v.supported and v.confidence >= 0.8
    }
    quotes = " ".join(
        citation.quote
        for claim in outcome.claims.claims
        if claim.claim_id in supported and claim.claim_id in outcome.draft.claim_ids
        for citation in claim.citations
        if citation.source_id == document.source_id and citation.url == document.url
    )
    text = " ".join(quotes.casefold().split())
    if (document.source_id == "cukarica_news"
            and place.get("time_basis") == "source_cukarica_main_stage"):
        starts_at = datetime.fromisoformat(place["starts_at"])
        return cukarica_stage_start(text, corroborated_year=starts_at.year) == starts_at.replace(
            tzinfo=None,
        )
    dob = document.source_id == "venue_youth_centre" and place.get("time_basis") in {
        "source_dob_concert_header",
        "source_dob_linked_concert",
    }
    venue_matches = " ".join(place["venue"]["name"].casefold().split()) in text
    if dob:
        venue_matches = re.search(r"\bdom(?:u)? omladine beograd(?:a)?\b", text) is not None
    if not venue_matches:
        return False
    months = (
        "januar",
        "februar",
        "mart",
        "april",
        "maj",
        "jun",
        "jul",
        "avgust",
        "septembar",
        "oktobar",
        "novembar",
        "decembar",
    )
    for key in ("starts_at", "ends_at"):
        if place.get(key) is None:
            continue
        value = datetime.fromisoformat(place[key])
        date_forms = (
            value.date().isoformat(),
            f"{value.day:02d}.{value.month:02d}.{value.year}",
            f"{value.day}.{value.month}.{value.year}",
        )
        month = months[value.month - 1]
        stem = month[:-2] if month.endswith("ar") else month
        verbal = rf"(?<!\d){value.day}\.\s*{stem}\w*\s+{value.year}(?!\d)"
        has_date = any(form in text for form in date_forms) or re.search(verbal, text) is not None
        if dob and not has_date:
            # Узкий адаптер уже связал лид с единственной карточкой того же концерта
            # в том же HTML. В цитате допустимо отсутствие года, но не другой год.
            partial = re.search(rf"(?<!\d){value.day}\.\s*{stem}\w*(?:\s+(20\d{{2}}))?", text)
            has_date = partial is not None and partial[1] in (None, str(value.year))
        clock = rf"(?<!\d)0?{value.hour}(?:[:.]{value.minute:02d})(?!\d)"
        whole_hour = rf"(?<!\d)0?{value.hour}\s+(?:časova|часова|h)(?!\w)"
        has_time = re.search(clock, text) or (value.minute == 0 and re.search(whole_hour, text))
        if not has_date or not has_time:
            return False
    return True


def export_events(session: Session, *, observed_at: datetime, city: str = "belgrade") -> EventsFeed:
    if observed_at.tzinfo is None or city not in PUBLIC_CHANNELS:
        raise ValueError("explicit city channel and aware snapshot time required")
    profile = load_city_profile(city)
    policy = json.loads(Path(f"cities/{city}/pin-policy.json").read_text(encoding="utf-8"))
    rows = session.execute(
        select(RawItem, EditorialRun, Post)
        .join(Source, Source.id == RawItem.source_id)
        .join(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
        .join(Post, Post.id == EditorialRun.post_id)
        .where(
            Source.city == city,
            Source.ownership == "official",
            Source.enabled.is_(True),
            Post.city == city,
            Post.category == "events",
            Post.published_at.is_not(None),
            Post.published_at <= observed_at,
            Post.telegram_message_id > 0,
            EditorialRun.delivery_status == "sent",
        )
        .order_by(Post.published_at.desc(), Post.id.desc())
        .limit(MAX_EVENTS * 2)
    )
    events: dict[str, PublicEvent] = {}
    for item, run, post in rows:
        try:
            package = _reviewed_package(session, run, post, item)
            if package is None:
                continue
            documents = tuple(package.event.documents) if package.event else (package.evidence,)
            ids = [d.raw_item_id for d in package.event.documents] if package.event else [item.id]
            attested = package.presentation.event_place if package.presentation else None
            for document in documents:
                source = session.get(Source, document.source_id)
                if source is None or public_article_url(document.url, source.url) is None:
                    continue
                place = resolve_saved_event_place(
                    session, document, ids, profile=profile, legacy_published=attested is None,
                )
                if place is None:
                    continue
                if attested is not None:
                    if place != attested:
                        continue
                elif not _backfill_claimed(package, document, place):
                    continue
                event = PublicEvent(
                    id=place["id"],
                    title=post.title,
                    source_url=document.url,
                    post_url=f"https://t.me/{PUBLIC_CHANNELS[city]}/{post.telegram_message_id}",
                    starts_at=place["starts_at"],
                    ends_at=place["ends_at"],
                    expires_at=place["expires_at"],
                    timezone=place["timezone"],
                    archived=datetime.fromisoformat(place["expires_at"]) <= observed_at,
                    venue=Venue.model_validate(place["venue"]),
                    coordinate_source_url=place["coordinate_source_url"],
                    venue_source_url=place["venue_source_url"],
                    schedule_source_url=place.get("schedule_source_url"),
                    sponsored=active_map_ad(policy, post.telegram_message_id or 0, observed_at),
                )
                events.setdefault(event.id, event)
                break
        except (ValueError, TypeError, KeyError, OverflowError):
            # Повреждённый исторический пакет не раскрывается и не ломает публичный фид.
            continue
        if len(events) >= MAX_EVENTS:
            break
    return EventsFeed(generated_at=observed_at.astimezone(UTC), events=tuple(events.values()))


def export_events_snapshot(database: Path, output: Path, observed_at: str) -> None:
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    try:
        with Session(engine) as session:
            feed = export_events(session, observed_at=datetime.fromisoformat(observed_at))
        write_events_feed(output, feed)
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", type=Path, required=True)
    args = parser.parse_args()
    read_events_feed(args.validate.read_bytes())


if __name__ == "__main__":
    main()
