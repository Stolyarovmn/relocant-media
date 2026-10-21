"""Partial observed counters with real ages, never inferred missing engagement."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from relocant_media.analytics.public_preview import parse_counter

CHANNEL = -1003967014133
HORIZONS = ((24, 2), (48, 3), (168, 12))


def _aware(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None:
        raise ValueError("observation time must be timezone-aware")
    return result.astimezone(UTC)


def _count(value: Any) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("counter must be a nonnegative integer or unknown")
    return value


def normalize_observation(
    observation: dict[str, Any], metadata: list[dict[str, Any]], *, now: datetime | None = None
) -> dict[str, Any]:
    if observation.get("channel_id") != CHANNEL or observation.get("source") != "telegram_web_ui":
        raise ValueError("unsupported own-channel observation")
    observed = _aware(observation["observed_at"])
    if observed > (now or datetime.now(UTC)) + timedelta(minutes=5):
        raise ValueError("observation is in the future")
    subscribers = _count(observation.get("subscribers"))
    by_id = {row["message_id"]: row for row in metadata}
    posts, seen = [], set()
    for raw in observation["posts"]:
        mid = raw["message_id"]
        if type(mid) is not int or mid <= 0 or mid in seen or mid not in by_id:
            raise ValueError("unknown or duplicate channel message")
        seen.add(mid)
        item = by_id[mid]
        published = _aware(item["published_at"])
        age = (observed - published).total_seconds() / 3600
        if age < 0:
            raise ValueError("message was not published at observation time")
        match = re.fullmatch(r"Views:\s*(.+)", raw.get("views_raw") or "")
        views = parse_counter(match[1]) if match else None
        horizon = next((h for h, tolerance in HORIZONS if abs(age - h) <= tolerance), None)
        corrected = item.get("has_correction") is True
        posts.append(
            dict(
                message_id=mid,
                post_id=item["post_id"],
                url=f"https://t.me/c/3967014133/{mid}",
                published_at=published.isoformat(),
                observed_at=observed.isoformat(),
                age_hours=round(age, 3),
                horizon_hours=horizon,
                category=item["category"],
                editorial_rule_version=item["editorial_rule_version"],
                views=views.value if views else None,
                views_approximate=views.approximate if views else None,
                reactions=_count(raw.get("reactions")),
                comments=_count(raw.get("comments")),
                forwards=_count(raw.get("forwards")),
                subscribers=subscribers,
                has_correction=corrected,
                comparable=horizon is not None
                and views is not None
                and not corrected
                and bool(item["editorial_rule_version"]),
            )
        )
    return dict(
        schema_version=1,
        source="telegram_web_ui",
        channel_id=CHANNEL,
        observed_at=observed.isoformat(),
        subscribers=subscribers,
        posts=posts,
        automatic_adaptation=False,
    )


def save_observation(directory: Path, payload: dict[str, Any]) -> Path:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    stamp = _aware(payload["observed_at"]).strftime("%Y%m%dT%H%M%S")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}-{digest[:12]}.json"
    if path.exists():
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError("observation collision")
        return path
    with path.open("x", encoding="utf-8") as stream:
        stream.write(encoded)
    return path


def feedback_summary(observations: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [post for item in observations for post in item.get("posts", [])]
    comparable = {p["message_id"] for p in rows if p.get("comparable") is True}
    return dict(
        observations=len(observations),
        observed_posts=len({p["message_id"] for p in rows}),
        comparable_posts=len(comparable),
        minimum_baseline_posts=30,
        status="collecting_baseline" if len(comparable) < 30 else "requires_controlled_comparison",
        automatic_adaptation=False,
        causal_growth_claim=False,
        missing_engagement_fields=[
            key
            for key in ("reactions", "comments", "forwards")
            if any(p.get(key) is None for p in rows)
        ],
    )
