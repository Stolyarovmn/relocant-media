"""Small public aggregates for a dated editorial brief."""

from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import EditorialRun, Post, PostSnapshot

LINK_LIMIT = 8
Count = Annotated[int, Field(strict=True, ge=0)]
MessageId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]


class Edition(BaseModel):
    status: Literal["sent", "pending", "unconfirmed", "not_scheduled"]
    message_id: MessageId | None = None

    @model_validator(mode="after")
    def confirmed_receipt(self) -> Self:
        if (self.status == "sent") != (self.message_id is not None):
            raise ValueError("digest receipt and status disagree")
        return self


class BriefDay(BaseModel):
    local_date: date
    post_count: Count
    message_ids: Annotated[list[MessageId], Field(max_length=LINK_LIMIT)]
    views: Count | None
    measured_posts: Count
    morning: Edition | None = None
    evening: Edition | None = None

    @model_validator(mode="after")
    def measurement_scope(self) -> Self:
        if (
            len(self.message_ids) > self.post_count
            or len(set(self.message_ids)) != len(self.message_ids)
            or self.measured_posts > self.post_count
            or (self.views is not None) != (self.measured_posts > 0)
        ):
            raise ValueError("daily aggregates disagree")
        return self


class BriefSnapshot(BaseModel):
    timezone: str
    days: Annotated[list[BriefDay], Field(max_length=2)]

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("unknown local timezone") from None
        return value

    @field_validator("days")
    @classmethod
    def unique_days(cls, days: list[BriefDay]) -> list[BriefDay]:
        if len(days) != len({day.local_date for day in days}):
            raise ValueError("duplicate report date")
        return days


def public_daily_brief(value: object) -> dict[str, Any] | None:
    """Only counts, dates and positive public Telegram message identifiers survive."""
    try:
        return BriefSnapshot.model_validate(value).model_dump(mode="json", exclude_unset=True)
    except ValidationError:
        return None


def collect_daily_brief(
    session: Session, city: str, *, now: datetime, zone: ZoneInfo,
) -> dict[str, Any]:
    """Use confirmed sends during two local days and the latest view reading for each."""
    today = now.astimezone(zone).date()
    yesterday = today - timedelta(days=1)
    days: dict[date, dict[int, int]] = {yesterday: {}, today: {}}
    seen: set[int] = set()
    sent_after = datetime.combine(yesterday, time.min, zone)
    for post_id, message_id, sent_at in session.execute(
        select(Post.id, Post.telegram_message_id, Post.published_at)
        .join(EditorialRun, EditorialRun.post_id == Post.id)
        .where(Post.city == city, EditorialRun.delivery_status == "sent",
               Post.telegram_message_id > 0, Post.published_at.between(sent_after, now))
        .order_by(Post.published_at, Post.id)
    ):
        if message_id is not None and sent_at is not None and message_id not in seen:
            seen.add(message_id)
            days[sent_at.astimezone(zone).date()][message_id] = post_id
    post_ids = [post_id for posts in days.values() for post_id in posts.values()]
    latest_views: dict[int, int] = {}
    if post_ids:
        last_reading = (
            select(PostSnapshot.captured_at)
            .where(PostSnapshot.post_id == Post.id, PostSnapshot.captured_at <= now,
                   PostSnapshot.captured_at >= Post.published_at)
            .order_by(PostSnapshot.captured_at.desc()).limit(1).scalar_subquery()
            .correlate(Post)
        )
        readings = session.execute(
            select(Post.id, PostSnapshot.views).join(
                PostSnapshot,
                (PostSnapshot.post_id == Post.id) & (PostSnapshot.captured_at == last_reading),
            ).where(Post.id.in_(post_ids), PostSnapshot.views >= 0)
        )
        latest_views = {post_id: views for post_id, views in readings}
    measured_days = []
    for calendar_day, posts in days.items():
        views = [latest_views[post_id] for post_id in posts.values() if post_id in latest_views]
        measured_days.append(BriefDay(
            local_date=calendar_day, post_count=len(posts), message_ids=list(posts)[-LINK_LIMIT:],
            views=sum(views) if views else None, measured_posts=len(views),
        ))
    return BriefSnapshot(timezone=zone.key, days=measured_days).model_dump(
        mode="json", exclude_unset=True,
    )
