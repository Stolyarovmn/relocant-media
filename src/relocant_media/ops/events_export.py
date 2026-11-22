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

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StringConstraints,
    TypeAdapter,
    field_validator,
    model_serializer,
    model_validator,
)
from sqlalchemy import create_engine, or_, select
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
from relocant_media.domain.dedup import canonicalize_url
from relocant_media.domain.map_links import MAP_ARCHIVE_AGE, map_event_id, map_occurrence_id
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.ops.newsroom_export import public_article_url
from relocant_media.services.event_place import event_occurrences, resolve_saved_event_place
from relocant_media.services.event_place_adapters import (
    CUKARICA_GALLERY,
    CUKARICA_STAGE,
    cukarica_venue_start,
)
from relocant_media.services.pin_policy import active_map_ad
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source, Submission

MAX_EVENTS = 1000
MAX_FEED_BYTES = 2_000_000
MAX_EVENT_COURSES = 4
MAX_COURSE_POINTS = 4096
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


CoursePoint = tuple[
    Annotated[float, Field(strict=True, ge=19.8, le=21.1, allow_inf_nan=False)],
    Annotated[float, Field(strict=True, ge=44.2, le=45.2, allow_inf_nan=False)],
]


class PublicCourse(PublicModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    coordinates: tuple[CoursePoint, ...] = Field(min_length=2, max_length=MAX_COURSE_POINTS)

    @model_validator(mode="after")
    def a_path(self) -> PublicCourse:
        if len(set(self.coordinates)) < 2:
            raise ValueError("a course requires distinct points")
        return self


Courses = Annotated[tuple[PublicCourse, ...], Field(max_length=MAX_EVENT_COURSES)]
_COURSES = TypeAdapter(Courses)


class PublicEvent(PublicModel):
    id: str = Field(pattern=r"^event_[0-9a-f]{20}$")
    kind: Literal["event"] = "event"
    title: Label
    source_url: str
    post_url: str
    published_at: AwareDatetime | None = None
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    expires_at: AwareDatetime
    timezone: str
    archived: bool
    venue: Venue
    coordinate_source_url: str
    venue_source_url: str
    schedule_source_url: str | None = None
    visitor_hours: Label | None = None
    schedule_note: Label | None = None
    sponsored: bool = False
    group_id: str | None = Field(default=None, pattern=r"^event_[0-9a-f]{20}$")
    occurrence_key: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,100}$")
    venue_note: Label | None = None
    courses: Courses = ()

    @model_serializer(mode="wrap")
    def serialize_optional_fields(
        self, handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        result: dict[str, Any] = handler(self)
        if not self.courses:
            result.pop("courses", None)
        if self.published_at is None:
            result.pop("published_at", None)
        return result

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
        if len({course.name.casefold() for course in self.courses}) != len(self.courses):
            raise ValueError("course names must be distinct")
        zone = ZoneInfo(self.timezone)
        expected = map_event_id(self.source_url)
        if self.group_id is not None:
            if self.group_id != expected or self.occurrence_key is None:
                raise ValueError("event occurrence requires its source group and key")
            expected = map_occurrence_id(self.source_url, self.occurrence_key)
        elif self.occurrence_key is not None:
            raise ValueError("event occurrence requires a group")
        if self.id != expected:
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


class ReviewedMapEvent(PublicModel):
    message_id: int = Field(gt=0)
    post_title: Label
    title: Label
    source_url: str
    starts_at: AwareDatetime
    ends_at: AwareDatetime | None = None
    venue: Venue
    coordinate_source_url: str
    venue_source_url: str
    schedule_source_url: str
    visitor_hours: Label | None = None
    schedule_note: Label | None = None
    evidence_urls: tuple[str, ...] = ()
    # Проверенная трасса или маршрут события (забег, шествие) из первоисточника.
    courses: Courses = ()
    # Площадка или сеанс одного события из одного поста; см. docs/EVENT_MAP_COMPLETENESS.md.
    occurrence_key: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,100}$")

    def card(
        self, city: str, timezone: str, now: datetime, *, published_at: datetime | None = None,
    ) -> PublicEvent:
        if not 44.2 <= self.venue.latitude <= 45.2 or not 19.8 <= self.venue.longitude <= 21.1:
            raise ValueError("reviewed venue outside the supported Belgrade map")
        expires = self.ends_at or datetime.combine(
            self.starts_at.astimezone(ZoneInfo(timezone)).date() + timedelta(days=1),
            time.min, ZoneInfo(timezone),
        )
        return PublicEvent(
            **self.model_dump(exclude={"message_id", "post_title", "evidence_urls"}),
            id=(map_occurrence_id(self.source_url, self.occurrence_key)
                if self.occurrence_key else map_event_id(self.source_url)),
            group_id=map_event_id(self.source_url) if self.occurrence_key else None,
            post_url=f"https://t.me/{PUBLIC_CHANNELS[city]}/{self.message_id}",
            published_at=published_at,
            expires_at=expires, timezone=timezone, archived=expires <= now,
        )


def validate_reviewed_groups(records: tuple[ReviewedMapEvent, ...]) -> None:
    """Точки одного события связываются только внутри одного поста и одного первоисточника."""
    by_post: dict[int, list[ReviewedMapEvent]] = {}
    owner: dict[str, int] = {}
    for record in records:
        by_post.setdefault(record.message_id, []).append(record)
        # ID карточки и группы строятся из канонического источника: один источник — один пост.
        source = canonicalize_url(record.source_url)
        if owner.setdefault(source, record.message_id) != record.message_id:
            raise ValueError(f"source {source} is bound to posts {owner[source]} "
                             f"and {record.message_id}")
    for message_id, group in by_post.items():
        keys = [record.occurrence_key for record in group]
        if len(group) == 1:
            if keys[0] is not None:
                raise ValueError(f"post {message_id}: occurrence_key needs a second point")
            continue
        if (None in keys or len(set(keys)) != len(keys)
                or len({(r.source_url, r.post_title) for r in group}) != 1):
            raise ValueError(
                f"post {message_id}: group needs one source, one post title and unique keys")
        if len({(r.venue.latitude, r.venue.longitude, r.starts_at) for r in group}) != len(group):
            raise ValueError(f"post {message_id}: duplicate place and time in one group")


class SponsoredPlace(PublicModel):
    id: str = Field(pattern=r"^event_[0-9a-f]{20}$")
    kind: Literal["sponsored_place"] = "sponsored_place"
    title: Label
    source_url: str
    post_url: str
    starts_at: AwareDatetime
    expires_at: AwareDatetime
    timezone: str
    venue: Venue

    @field_validator("source_url")
    @classmethod
    def public_url(cls, value: str) -> str:
        return PublicEvent.public_url(value)

    @field_validator("post_url")
    @classmethod
    def published_url(cls, value: str) -> str:
        return PublicEvent.published_url(value)

    @model_validator(mode="after")
    def consistent(self) -> SponsoredPlace:
        if self.id != map_event_id(self.post_url) or self.starts_at >= self.expires_at:
            raise ValueError("invalid paid place identity or display period")
        ZoneInfo(self.timezone)
        if not 44.2 <= self.venue.latitude <= 45.2 or not 19.8 <= self.venue.longitude <= 21.1:
            raise ValueError("paid place outside the supported Belgrade map")
        return self


class EventsFeed(PublicModel):
    version: Literal[1] = 1
    generated_at: AwareDatetime | None = None
    events: tuple[PublicEvent, ...] = Field(default=(), max_length=MAX_EVENTS)
    sponsored_places: tuple[SponsoredPlace, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def unique_events(self) -> EventsFeed:
        cards: tuple[PublicEvent | SponsoredPlace, ...] = (*self.events, *self.sponsored_places)
        if len({card.id for card in cards}) != len(cards):
            raise ValueError("duplicate event identifier")
        if cards and self.generated_at is None:
            raise ValueError("event feed timestamp required")
        if self.generated_at is not None and any(
            event.archived != (event.expires_at <= self.generated_at) for event in self.events
        ):
            raise ValueError("event archive state differs from snapshot time")
        if self.generated_at is not None and any(
            not row.starts_at <= self.generated_at < row.expires_at
            for row in self.sponsored_places
        ):
            raise ValueError("paid place outside campaign period")
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
        or package.outcome.ranking.category not in {"events", "culture", "sport"}
        or package.outcome.ranking.category != post.category
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
    if (document.source_id == "cukarica_news" and place.get("time_basis") in {
        "source_cukarica_main_stage", "source_cukarica_gallery",
    }):
        starts_at = datetime.fromisoformat(place["starts_at"])
        venue = (CUKARICA_GALLERY if place["time_basis"] == "source_cukarica_gallery"
                 else CUKARICA_STAGE)
        claimed = cukarica_venue_start(text, venue, corroborated_year=starts_at.year)
        return claimed == starts_at.replace(tzinfo=None)
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


def _sponsored_places(
    policy: dict[str, Any], city: str, timezone: str, now: datetime,
) -> tuple[SponsoredPlace, ...]:
    result: dict[str, SponsoredPlace] = {}
    for row in policy.get("advertisements", []):
        if not isinstance(row, dict) or not isinstance(row.get("place"), dict):
            continue
        message_id = row.get("channel_message_id")
        if type(message_id) is not int or not active_map_ad(policy, message_id, now):
            continue
        post = f"https://t.me/{PUBLIC_CHANNELS[city]}/{message_id}"
        try:
            place = row["place"]
            record = SponsoredPlace(
                id=map_event_id(post), title=place["title"], source_url=place["source_url"],
                post_url=post,
                starts_at=TypeAdapter(AwareDatetime).validate_python(row["starts_at"]),
                expires_at=TypeAdapter(AwareDatetime).validate_python(row["ends_at"]),
                timezone=timezone, venue=Venue.model_validate(place["venue"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        result.setdefault(record.id, record)
        if len(result) >= 100:
            break
    return tuple(result.values())


def _reviewed_events(
    session: Session, city: str, timezone: str, now: datetime,
) -> dict[str, PublicEvent]:
    registry = Path(f"cities/{city}/reviewed-map-events.json")
    if not registry.is_file():
        return {}
    records = TypeAdapter(tuple[ReviewedMapEvent, ...]).validate_json(registry.read_bytes())
    validate_reviewed_groups(records)
    events = {}
    for record in records:
        published_at = session.scalar(
            select(Post.published_at)
            .join(EditorialRun, EditorialRun.post_id == Post.id)
            .join(RawItem, RawItem.id == EditorialRun.raw_item_id)
            .where(
                Post.city == city, Post.title == record.post_title,
                Post.telegram_message_id == record.message_id, Post.published_at <= now,
                or_(RawItem.url == record.source_url,
                    RawItem.canonical_url == canonicalize_url(record.source_url)),
                EditorialRun.delivery_status == "sent",
            )
            .limit(1)
        )
        if published_at is not None:
            card = record.card(city, timezone, now, published_at=published_at)
            events[card.id] = card
    return events


def export_events(session: Session, *, observed_at: datetime, city: str = "belgrade") -> EventsFeed:
    if observed_at.tzinfo is None or city not in PUBLIC_CHANNELS:
        raise ValueError("explicit city channel and aware snapshot time required")
    archive_cutoff = observed_at.astimezone(UTC) - MAP_ARCHIVE_AGE
    profile = load_city_profile(city)
    policy = json.loads(Path(f"cities/{city}/pin-policy.json").read_text(encoding="utf-8"))
    rows = session.execute(
        select(RawItem, EditorialRun, Post)
        .join(Source, Source.id == RawItem.source_id)
        .join(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
        .join(Post, Post.id == EditorialRun.post_id)
        .where(
            Source.city == city,
            or_(
                Source.ownership == "official",
                EditorialRun.outcome["_package"]["presentation"]["event_place"]["source_url"]
                .as_string().is_not(None),
            ),
            Source.enabled.is_(True),
            Post.city == city,
            Post.category.in_(("events", "culture", "sport")),
            Post.published_at.is_not(None),
            Post.published_at <= observed_at,
            Post.telegram_message_id > 0,
            EditorialRun.delivery_status == "sent",
        )
        .order_by(Post.published_at.desc(), Post.id.desc())
        .limit(MAX_EVENTS * 2)
    )
    reviewed = _reviewed_events(session, city, profile.timezone, observed_at)
    reviewed_posts = {event.post_url for event in reviewed.values()}
    events = {key: event for key, event in reviewed.items() if event.expires_at >= archive_cutoff}
    for item, run, post in rows:
        if len(events) >= MAX_EVENTS:
            break
        post_url = f"https://t.me/{PUBLIC_CHANNELS[city]}/{post.telegram_message_id}"
        if post_url in reviewed_posts:
            continue
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
                    session, document, ids, profile=profile,
                )
                if place is None:
                    continue
                if attested is not None:
                    if place != attested:
                        continue
                elif not _backfill_claimed(package, document, place):
                    continue
                cards = []
                for slot in event_occurrences(place):
                    expires_at = datetime.fromisoformat(slot["expires_at"])
                    if expires_at < archive_cutoff:
                        continue
                    courses = _COURSES.validate_python(slot.get("courses", ())) if attested else ()
                    cards.append(PublicEvent(
                        id=slot["id"],
                        group_id=slot.get("group_id"),
                        occurrence_key=slot.get("occurrence_key"),
                        title=post.title,
                        source_url=document.url,
                        post_url=post_url,
                        published_at=post.published_at,
                        starts_at=slot["starts_at"],
                        ends_at=slot["ends_at"],
                        expires_at=expires_at,
                        timezone=slot["timezone"],
                        archived=expires_at <= observed_at,
                        venue=Venue.model_validate(slot["venue"]),
                        coordinate_source_url=slot["coordinate_source_url"],
                        venue_source_url=slot["venue_source_url"],
                        schedule_source_url=slot.get("schedule_source_url"),
                        venue_note=slot.get("venue_note"),
                        courses=courses,
                        sponsored=active_map_ad(policy, post.telegram_message_id or 0, observed_at),
                    ))
                # Неполная программа не должна попадать в фид при ограничении размера.
                if len(events) + len(cards) > MAX_EVENTS:
                    continue
                for event in cards:
                    events.setdefault(event.id, event)
                break
        except (ValueError, TypeError, KeyError, OverflowError):
            # Повреждённый исторический пакет не раскрывается и не ломает публичный фид.
            continue
    places = _sponsored_places(policy, city, profile.timezone, observed_at)
    return EventsFeed(generated_at=observed_at.astimezone(UTC), events=tuple(events.values()),
                      sponsored_places=places)


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
