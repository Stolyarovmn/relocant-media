"""Отправленная авиановость, к которой может относиться новый материал."""
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.linked_update import PublishedPostReference
from relocant_media.domain.flight_notices import (
    FLIGHT_REVIEW_REASON,
    find_related_flight_cancellation,
)
from relocant_media.services.editorial import DeliveryBlockedError
from relocant_media.services.published_text import effective_published_text
from relocant_media.storage.models import EditorialRun, Post


@dataclass(frozen=True)
class FlightParent:
    raw_item_id: int
    message_id: int


class FlightReviewRequired(DeliveryBlockedError):
    def __init__(self, parent: FlightParent) -> None:
        super().__init__("flight cancellation requires verified linked update "
                         f"to post {parent.message_id}")
        self.parent = parent


def guard_flight_delivery(
    session: Session, post: Post, *, reply_to: int | None, now: datetime,
) -> None:
    if reply_to is not None:
        return
    parents: dict[int, FlightParent] = {}

    def history() -> Iterator[tuple[int, str, str, datetime | None]]:
        rows = session.execute(select(Post, EditorialRun).join(
            EditorialRun, EditorialRun.post_id == Post.id,
        ).where(
            Post.city == post.city, Post.id != post.id, Post.telegram_message_id > 0,
            Post.published_at.between(now - timedelta(hours=72), now),
            EditorialRun.delivery_status == "sent", EditorialRun.staging_message_id.is_(None),
        ).order_by(Post.published_at, Post.id))
        for previous, run in rows:
            timestamp, message_id = previous.published_at, previous.telegram_message_id
            assert timestamp is not None and message_id is not None
            effective = effective_published_text(previous, run)
            parents[message_id] = FlightParent(run.raw_item_id, message_id)
            yield message_id, effective.title, effective.body, (
                timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=UTC))

    related = find_related_flight_cancellation(post.title, post.body, history(), now=now)
    if related is not None:
        raise FlightReviewRequired(parents[related])


def flight_review_marker(run: EditorialRun | None) -> FlightParent | None:
    if (run is None or run.outcome.get("reason") != FLIGHT_REVIEW_REASON
            or run.outcome.get("status") != "human_review"
            or run.delivery_status != "pending" or run.staging_message_id is not None):
        return None
    parent = run.outcome.get("_flight_review")
    if not isinstance(parent, dict):
        return None
    raw_id, message_id = parent.get("raw_item_id"), parent.get("message_id")
    if type(raw_id) is not int or type(message_id) is not int or min(raw_id, message_id) < 1:
        return None
    return FlightParent(raw_id, message_id)


def flight_review_reference(
    session: Session, run: EditorialRun | None, *, city: str, now: datetime,
) -> PublishedPostReference | None:
    marker = flight_review_marker(run)
    if marker is None:
        return None
    assert run is not None
    post = session.get(Post, run.post_id) if run.post_id is not None else None
    if (post is None or post.status != "human_review" or post.published_at is not None
            or post.telegram_message_id is not None or post.city != city
            or any(k in run.outcome for k in ("_reader_submission_id", "_rich_delivery",
                                              "_media_delivery"))):
        raise ValueError("Материал недоступен для связанного редакционного разбора")
    from relocant_media.services.linked_updates import published_post_reference

    reference = published_post_reference(session, marker.raw_item_id, city=city, now=now)
    if reference.message_id != marker.message_id:
        raise ValueError("Квитанция исходной авиановости изменилась")
    return reference
