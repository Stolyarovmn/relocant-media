"""Profit-vector KPIs derived only from confirmed observations; unknown stays None."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import ChannelSnapshot, Post


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _subscribers(session: Session, city: str, now: datetime) -> dict[str, Any] | None:
    rows = sorted(
        ((_aware(row.captured_at), row.subscribers) for row in session.scalars(
            select(ChannelSnapshot).where(
                ChannelSnapshot.city == city, ChannelSnapshot.kind == "observed",
            )
        ) if _aware(row.captured_at) <= now),
        key=lambda row: row[0],
    )
    if not rows:
        return None
    observed_at, count = rows[-1]

    def delta(window: timedelta) -> int | None:
        older = [value for captured, value in rows if captured <= now - window]
        return count - older[-1] if older else None

    return {"count": count, "observed_at": observed_at.isoformat(),
            "delta_24h": delta(timedelta(hours=24)), "delta_7d": delta(timedelta(days=7))}


def _published(session: Session, city: str, now: datetime) -> dict[str, Any]:
    times = [
        _aware(row) for row in session.scalars(
            select(Post.published_at).where(
                Post.city == city, Post.telegram_message_id.is_not(None),
                Post.published_at.is_not(None),
            )
        ) if row is not None and _aware(row) <= now
    ]
    last = max(times) if times else None
    return {
        "last_24h": sum(1 for value in times if value > now - timedelta(hours=24)),
        "last_7d": sum(1 for value in times if value > now - timedelta(days=7)),
        "last_published_at": last.isoformat() if last else None,
        "hours_since_last": round((now - last).total_seconds() / 3600, 1) if last else None,
    }


def compute_kpi(session: Session, city: str, *, now: datetime) -> dict[str, Any]:
    """Views, revenue and ad income are not observed here and are deliberately absent."""
    return {"subscribers": _subscribers(session, city, now),
            "published": _published(session, city, now)}


def record_subscriber_observation(
    session: Session, city: str, observation: dict[str, Any],
) -> None:
    count = observation.get("subscribers")
    if observation.get("status") != "observed" or type(count) is not int or count < 0:
        return
    captured = datetime.fromisoformat(observation["observed_at"])
    session.add(ChannelSnapshot(
        city=city, captured_at=captured, subscribers=count, kind="observed",
        source=str(observation.get("source") or "telegram_bot_api"),
    ))
    session.commit()


def _number(value: object) -> int | float | None:
    if type(value) is int or type(value) is float:
        return value
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and len(value) <= 40 else None


def public_kpi(value: object) -> dict[str, Any] | None:
    """Rebuild a KPI block from allowlisted, typed fields of an untrusted snapshot."""
    if not isinstance(value, dict):
        return None
    audience, published = value.get("subscribers"), value.get("published")
    return {
        "subscribers": None if not isinstance(audience, dict) else {
            "count": _number(audience.get("count")),
            "observed_at": _text(audience.get("observed_at")),
            "delta_24h": _number(audience.get("delta_24h")),
            "delta_7d": _number(audience.get("delta_7d")),
        },
        "published": None if not isinstance(published, dict) else {
            "last_24h": _number(published.get("last_24h")),
            "last_7d": _number(published.get("last_7d")),
            "last_published_at": _text(published.get("last_published_at")),
            "hours_since_last": _number(published.get("hours_since_last")),
        },
    }
