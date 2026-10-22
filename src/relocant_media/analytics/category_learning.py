"""Read-only category learning signals from published posts and fixed-horizon snapshots."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import EditorialRun, Post, PostSnapshot, RawItem


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("category-learning timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


def build_category_learning_report(
    session: Session,
    *,
    city: str,
    since: datetime,
    until: datetime,
    horizon_hours: int = 24,
    minimum_samples: int = 5,
) -> dict[str, Any]:
    """Describe category performance without changing editorial or discovery policy.

    Posts are selected by their real publication time. Audience comparison uses at most one
    persisted PostSnapshot per public post at the exact requested horizon; if more than one
    exists, the newest capture wins. Missing snapshots remain missing rather than being imputed.
    """
    start = _utc(since)
    end = _utc(until)
    if start >= end:
        raise ValueError("category-learning since must be before until")
    if horizon_hours <= 0:
        raise ValueError("horizon_hours must be positive")
    if minimum_samples < 2:
        raise ValueError("minimum_samples must be at least 2")

    posts = list(
        session.scalars(
            select(Post)
            .where(
                Post.city == city,
                Post.published_at.is_not(None),
                Post.published_at >= start,
                Post.published_at < end,
            )
            .order_by(Post.id)
        )
    )
    post_ids = [post.id for post in posts]

    run_by_post: dict[int, tuple[str, str]] = {}
    if post_ids:
        run_rows = session.execute(
            select(EditorialRun.post_id, EditorialRun.delivery_status, RawItem.source_id)
            .join(RawItem, RawItem.id == EditorialRun.raw_item_id)
            .where(EditorialRun.post_id.in_(post_ids))
        ).all()
        for post_id, delivery_status, source_id in run_rows:
            if isinstance(post_id, int):
                run_by_post[post_id] = (delivery_status, source_id)

    latest_snapshot: dict[int, PostSnapshot] = {}
    if post_ids:
        snapshots = list(
            session.scalars(
                select(PostSnapshot)
                .where(
                    PostSnapshot.post_id.in_(post_ids),
                    PostSnapshot.horizon_hours == horizon_hours,
                )
                .order_by(PostSnapshot.post_id, PostSnapshot.captured_at)
            )
        )
        for snapshot in snapshots:
            latest_snapshot[snapshot.post_id] = snapshot

    category_state: dict[str, dict[str, Any]] = {}
    for post in posts:
        row = category_state.setdefault(
            post.category,
            {
                "category": post.category,
                "published_posts": 0,
                "public_sent": 0,
                "observed_posts": 0,
                "views": 0,
                "reactions": 0,
                "comments": 0,
                "forwards": 0,
                "anchor_sources": Counter(),
            },
        )
        row["published_posts"] += 1
        run = run_by_post.get(post.id)
        public_sent = (
            run is not None
            and run[0] == "sent"
            and isinstance(post.telegram_message_id, int)
            and post.telegram_message_id > 0
        )
        if not public_sent:
            continue

        row["public_sent"] += 1
        row["anchor_sources"][run[1]] += 1
        snapshot = latest_snapshot.get(post.id)
        if snapshot is None:
            continue
        row["observed_posts"] += 1
        row["views"] += snapshot.views
        row["reactions"] += snapshot.reactions
        row["comments"] += snapshot.comments
        row["forwards"] += snapshot.forwards

    categories: list[dict[str, Any]] = []
    for category in sorted(category_state):
        state = category_state[category]
        published_posts = int(state["published_posts"])
        public_sent = int(state["public_sent"])
        observed_posts = int(state["observed_posts"])
        views = int(state["views"])
        interactions = int(state["reactions"]) + int(state["comments"]) + int(state["forwards"])
        categories.append(
            {
                "category": category,
                "published_posts": published_posts,
                "public_sent": public_sent,
                "observed_posts": observed_posts,
                "snapshot_coverage": _rate(observed_posts, public_sent),
                "public_sent_from_published": _rate(public_sent, published_posts),
                "views": views,
                "interactions": interactions,
                "reactions": int(state["reactions"]),
                "comments": int(state["comments"]),
                "forwards": int(state["forwards"]),
                "views_per_observed_post": _rate(views, observed_posts),
                "engagement_per_view_observed": _rate(interactions, views),
                "anchor_sources": dict(sorted(state["anchor_sources"].items())),
            }
        )

    baseline_rows = [
        row
        for row in categories
        if row["observed_posts"] >= minimum_samples
        and row["views_per_observed_post"] is not None
        and row["engagement_per_view_observed"] is not None
    ]
    baseline: dict[str, Any] = {
        "eligible_categories": len(baseline_rows),
        "minimum_samples_per_category": minimum_samples,
        "views_per_post_median": None,
        "engagement_per_view_median": None,
    }
    if len(baseline_rows) >= 2:
        baseline["views_per_post_median"] = round(
            float(median(float(row["views_per_observed_post"]) for row in baseline_rows)),
            6,
        )
        baseline["engagement_per_view_median"] = round(
            float(median(float(row["engagement_per_view_observed"]) for row in baseline_rows)),
            6,
        )

    recommendations: list[dict[str, str]] = []
    baseline_ready = baseline["views_per_post_median"] is not None
    for row in categories:
        category = str(row["category"])
        if row["observed_posts"] < minimum_samples:
            recommendations.append(
                {
                    "category": category,
                    "class": "AUTO",
                    "code": "collect_fixed_horizon_samples",
                    "message": (
                        f"Collect more {horizon_hours}h snapshots for {category}; "
                        "do not change discovery priority yet."
                    ),
                }
            )
            continue
        if not baseline_ready:
            recommendations.append(
                {
                    "category": category,
                    "class": "AUTO",
                    "code": "collect_cross_category_baseline",
                    "message": "Collect another mature category before comparing discovery attention.",
                }
            )
            continue

        views_per_post = float(row["views_per_observed_post"])
        engagement = float(row["engagement_per_view_observed"])
        median_views = float(baseline["views_per_post_median"])
        median_engagement = float(baseline["engagement_per_view_median"])
        if views_per_post > median_views and engagement > median_engagement:
            code = "review_discovery_expansion"
            message = (
                f"{category} is above the observed category medians on reach and engagement; "
                "review whether discovery coverage should be expanded."
            )
        elif views_per_post < median_views and engagement < median_engagement:
            code = "review_discovery_efficiency"
            message = (
                f"{category} is below the observed category medians on reach and engagement; "
                "review source mix and topic usefulness before changing coverage."
            )
        else:
            code = "mixed_observed_signal"
            message = (
                f"{category} has a mixed observational signal; keep current discovery priority "
                "until more evidence is available."
            )
        recommendations.append(
            {
                "category": category,
                "class": "PR_REVIEW",
                "code": code,
                "message": message,
            }
        )

    return {
        "city": city,
        "window": {"since": start.isoformat(), "until": end.isoformat()},
        "horizon_hours": horizon_hours,
        "categories": categories,
        "baseline": baseline,
        "recommendations": recommendations,
        "automatic_adaptation": False,
        "editorial_hard_gates_unchanged": True,
        "causal_growth_claim": False,
        "limitations": [
            (
                "Category attribution starts only after a Post exists; rejected raw leads without "
                "a post cannot be assigned to a post category."
            ),
            "Anchor-source attribution uses the EditorialRun raw item only.",
            (
                "Only the newest persisted snapshot at the exact requested horizon is used per "
                "public post; missing snapshots are not imputed."
            ),
            "Cross-category medians are descriptive and do not establish causality.",
            (
                "Recommendations never change source schedules, auto_publish, high-risk policy, "
                "verification thresholds, or editorial rules automatically."
            ),
        ],
    }
