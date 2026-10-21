"""Bounded suggested-post review with deterministic safety floors and no delivery capability."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.ad_review import SuggestedPostReview
from relocant_media.storage.models import AdRequest

ALLOWED_AD_REVIEW_OUTCOMES = frozenset(
    {"accept_candidate", "counter_offer_candidate", "human_review", "reject"}
)
RESTRICTED_AD_FLAGS = frozenset(
    {"medicine", "investment", "credit", "gambling", "political", "unverifiable_earnings"}
)
HARD_REJECT_FLAGS = frozenset({"scam", "illegal_goods"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def _payload_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_flags(values: list[str]) -> list[str]:
    normalized = sorted({value.strip().casefold() for value in values if value.strip()})
    if len(normalized) != len(values):
        raise ValueError("risk_flags must be unique and non-empty")
    return normalized


def _normalize_reasons(values: list[str]) -> list[str]:
    normalized = [value.strip() for value in values if value.strip()]
    if not normalized:
        raise ValueError("at least one reason_code is required")
    if len(set(normalized)) != len(normalized):
        raise ValueError("reason_codes must be unique")
    return normalized


def _effective_outcome(
    *,
    proposed_outcome: str,
    risk_flags: list[str],
    audience_fit: bool,
    claims_verified: bool,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    flag_set = set(risk_flags)
    if flag_set & HARD_REJECT_FLAGS:
        return "reject", ["hard_reject_category"]
    if proposed_outcome == "reject":
        return "reject", reasons
    if flag_set & RESTRICTED_AD_FLAGS:
        return "human_review", ["restricted_category_requires_human_review"]
    if proposed_outcome == "accept_candidate" and not audience_fit:
        return "human_review", ["audience_fit_not_confirmed"]
    if proposed_outcome == "accept_candidate" and not claims_verified:
        return "human_review", ["claims_not_verified"]
    return proposed_outcome, reasons


def record_suggested_post_review(
    session: Session,
    *,
    ad_request_id: int,
    proposed_outcome: str,
    reason_codes: list[str],
    risk_flags: list[str],
    audience_fit: bool,
    claims_verified: bool,
    reviewer: str,
    idempotency_key: str,
    reviewer_metadata: dict[str, Any] | None = None,
    reviewed_at: datetime | None = None,
) -> SuggestedPostReview:
    """Record one review; safety floors may make the effective outcome more conservative."""
    normalized_outcome = proposed_outcome.strip().casefold()
    normalized_reviewer = reviewer.strip()
    normalized_key = idempotency_key.strip()
    if normalized_outcome not in ALLOWED_AD_REVIEW_OUTCOMES:
        raise ValueError("unsupported suggested-post review outcome")
    if not normalized_reviewer or not normalized_key:
        raise ValueError("reviewer and idempotency_key are required")
    if len(normalized_key) > 128:
        raise ValueError("idempotency_key is too long")

    request = session.get(AdRequest, ad_request_id)
    if request is None:
        raise ValueError("ad request does not exist")
    if request.payload.get("city") != "belgrade":
        raise ValueError("ad request is outside the configured city")

    normalized_flags = _normalize_flags(risk_flags)
    normalized_reasons = _normalize_reasons(reason_codes)
    effective, safety_reasons = _effective_outcome(
        proposed_outcome=normalized_outcome,
        risk_flags=normalized_flags,
        audience_fit=audience_fit,
        claims_verified=claims_verified,
    )
    for reason in safety_reasons:
        if reason not in normalized_reasons:
            normalized_reasons.append(reason)

    payload_hash = _payload_sha256(request.payload)
    metadata = reviewer_metadata or {}
    existing = session.scalar(
        select(SuggestedPostReview).where(SuggestedPostReview.idempotency_key == normalized_key)
    )
    if existing is not None:
        if (
            existing.ad_request_id != ad_request_id
            or existing.reviewed_payload_sha256 != payload_hash
            or existing.proposed_outcome != normalized_outcome
            or existing.effective_outcome != effective
            or existing.reason_codes != normalized_reasons
            or existing.risk_flags != normalized_flags
            or existing.audience_fit != audience_fit
            or existing.claims_verified != claims_verified
            or existing.reviewer != normalized_reviewer
            or existing.reviewer_metadata != metadata
        ):
            raise ValueError("idempotency_key already belongs to another suggested-post review")
        return existing

    row = SuggestedPostReview(
        ad_request_id=ad_request_id,
        reviewed_payload_sha256=payload_hash,
        proposed_outcome=normalized_outcome,
        effective_outcome=effective,
        reason_codes=normalized_reasons,
        risk_flags=normalized_flags,
        audience_fit=audience_fit,
        claims_verified=claims_verified,
        requires_owner_approval=True,
        reviewer=normalized_reviewer,
        reviewer_metadata=metadata,
        idempotency_key=normalized_key,
        reviewed_at=reviewed_at or datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def build_suggested_post_review_control(
    session: Session,
    *,
    city: str = "belgrade",
    limit: int = 100,
) -> dict[str, Any]:
    """Return privacy-safe advertiser review state without exposing request text or contacts."""
    if limit < 1:
        raise ValueError("limit must be positive")

    requests = list(
        session.scalars(
            select(AdRequest)
            .where(AdRequest.payload["city"].as_string() == city)
            .order_by(AdRequest.created_at.desc(), AdRequest.id.desc())
        )
    )
    ids = [request.id for request in requests]
    reviews_by_request: dict[int, list[SuggestedPostReview]] = {}
    if ids:
        for review in session.scalars(
            select(SuggestedPostReview)
            .where(SuggestedPostReview.ad_request_id.in_(ids))
            .order_by(
                SuggestedPostReview.ad_request_id,
                SuggestedPostReview.reviewed_at,
                SuggestedPostReview.id,
            )
        ):
            reviews_by_request.setdefault(review.ad_request_id, []).append(review)

    items: list[dict[str, Any]] = []
    pending = 0
    for request in requests:
        history = reviews_by_request.get(request.id, [])
        latest = history[-1] if history else None
        current_hash = _payload_sha256(request.payload)
        stale = latest is not None and latest.reviewed_payload_sha256 != current_hash
        needs_review = latest is None or stale or latest.effective_outcome == "human_review"
        if needs_review:
            pending += 1
        items.append(
            {
                "ad_request_id": request.id,
                "intake_status": request.status,
                "created_at": _utc(request.created_at).isoformat(),
                "review_status": (
                    "unreviewed"
                    if latest is None
                    else "stale"
                    if stale
                    else "human_review_required"
                    if latest.effective_outcome == "human_review"
                    else "candidate_reviewed"
                ),
                "latest_review": (
                    None
                    if latest is None
                    else {
                        "review_id": latest.id,
                        "proposed_outcome": latest.proposed_outcome,
                        "effective_outcome": latest.effective_outcome,
                        "reason_codes": latest.reason_codes,
                        "risk_flags": latest.risk_flags,
                        "audience_fit": latest.audience_fit,
                        "claims_verified": latest.claims_verified,
                        "requires_owner_approval": latest.requires_owner_approval,
                        "reviewer": latest.reviewer,
                        "reviewed_at": _utc(latest.reviewed_at).isoformat(),
                    }
                ),
                "review_count": len(history),
                "payload_changed_after_review": stale,
                "private_request_text_exposed": False,
                "advertiser_identity_exposed": False,
                "publication_capability": False,
                "spend_capability": False,
            }
        )

    items.sort(
        key=lambda item: (str(item["created_at"]), int(item["ad_request_id"])),
        reverse=True,
    )
    items.sort(
        key=lambda item: item["review_status"] not in {
            "unreviewed",
            "stale",
            "human_review_required",
        }
    )
    visible = items[:limit]
    return {
        "version": 1,
        "city": city,
        "total_requests": len(items),
        "requests_requiring_review": pending,
        "returned_requests": len(visible),
        "allowed_outcomes": sorted(ALLOWED_AD_REVIEW_OUTCOMES),
        "restricted_flags": sorted(RESTRICTED_AD_FLAGS),
        "items": visible,
        "safety": {
            "accept_candidate_is_publication_approval": False,
            "owner_approval_required_for_publication": True,
            "telegram_publication_capability": False,
            "ads_spend_capability": False,
            "private_request_text_in_control_output": False,
        },
    }
