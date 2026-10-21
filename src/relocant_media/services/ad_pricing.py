"""Owner-configured advertising pricing policy with fail-closed defaults.

This module can evaluate a commercial price candidate only. It cannot publish a Telegram post,
send an offer, charge an advertiser, or mutate Telegram Ads spend/bid/budget.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.ad_review import SuggestedPostReview
from relocant_media.storage.models import AdRequest


@dataclass(frozen=True, slots=True)
class AdPricingPackage:
    package_id: str
    base_price: Decimal
    minimum_without_owner: Decimal
    maximum_without_owner: Decimal


@dataclass(frozen=True, slots=True)
class AdPricingPolicy:
    version: str
    configured: bool
    currency: str | None
    approval_mode: str
    packages: dict[str, AdPricingPackage]
    policy_sha256: str


def _decimal(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be an explicit positive decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field} must be an explicit positive decimal") from exc
    if not result.is_finite() or result <= 0:
        raise ValueError(f"{field} must be an explicit positive decimal")
    return result


def _policy_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_ad_pricing_policy(path: Path) -> AdPricingPolicy:
    """Load an explicit policy. Missing or partial money configuration is rejected."""
    raw = path.read_bytes()
    parsed = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("ad pricing policy must be a YAML mapping")

    allowed_top = {"version", "configured", "currency", "packages", "approval"}
    unknown_top = set(parsed) - allowed_top
    if unknown_top:
        raise ValueError(f"unknown ad pricing policy keys: {sorted(unknown_top)}")

    version = parsed.get("version")
    configured = parsed.get("configured")
    currency = parsed.get("currency")
    packages_raw = parsed.get("packages")
    approval_raw = parsed.get("approval")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("pricing policy version is required")
    if not isinstance(configured, bool):
        raise ValueError("pricing policy configured must be boolean")
    if not isinstance(packages_raw, dict):
        raise ValueError("pricing policy packages must be a mapping")
    if not isinstance(approval_raw, dict):
        raise ValueError("pricing policy approval must be a mapping")
    if set(approval_raw) != {"mode"}:
        raise ValueError("pricing policy approval supports only explicit mode")
    approval_mode = approval_raw.get("mode")
    if approval_mode not in {"owner_always", "bounded"}:
        raise ValueError("pricing approval mode must be owner_always or bounded")

    if not configured:
        if currency is not None or packages_raw or approval_mode != "owner_always":
            raise ValueError(
                "unconfigured pricing must have null currency, no packages and owner_always mode"
            )
        return AdPricingPolicy(
            version=version.strip(),
            configured=False,
            currency=None,
            approval_mode="owner_always",
            packages={},
            policy_sha256=_policy_hash(raw),
        )

    if not isinstance(currency, str) or not currency.strip():
        raise ValueError("configured pricing requires an explicit currency")
    normalized_currency = currency.strip().upper()
    if len(normalized_currency) != 3 or not normalized_currency.isalpha():
        raise ValueError("currency must be an explicit three-letter code")
    if not packages_raw:
        raise ValueError("configured pricing requires at least one explicit package")

    packages: dict[str, AdPricingPackage] = {}
    expected_package_keys = {
        "base_price",
        "minimum_without_owner",
        "maximum_without_owner",
    }
    for raw_id, raw_package in packages_raw.items():
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise ValueError("pricing package id must be a non-empty string")
        package_id = raw_id.strip()
        if package_id in packages:
            raise ValueError("duplicate pricing package id")
        if not isinstance(raw_package, dict) or set(raw_package) != expected_package_keys:
            raise ValueError(
                f"package {package_id} requires base/minimum/maximum explicit prices"
            )
        base = _decimal(raw_package["base_price"], field=f"{package_id}.base_price")
        minimum = _decimal(
            raw_package["minimum_without_owner"],
            field=f"{package_id}.minimum_without_owner",
        )
        maximum = _decimal(
            raw_package["maximum_without_owner"],
            field=f"{package_id}.maximum_without_owner",
        )
        if not minimum <= base <= maximum:
            raise ValueError(
                f"package {package_id} requires minimum <= base_price <= maximum"
            )
        packages[package_id] = AdPricingPackage(
            package_id=package_id,
            base_price=base,
            minimum_without_owner=minimum,
            maximum_without_owner=maximum,
        )

    return AdPricingPolicy(
        version=version.strip(),
        configured=True,
        currency=normalized_currency,
        approval_mode=approval_mode,
        packages=packages,
        policy_sha256=_policy_hash(raw),
    )


def evaluate_ad_price_candidate(
    session: Session,
    *,
    policy: AdPricingPolicy,
    ad_request_id: int,
    package_id: str,
    candidate_price: Decimal | None,
) -> dict[str, object]:
    """Evaluate a price candidate without sending or approving a commercial offer."""
    if not policy.configured:
        return {
            "status": "owner_configuration_required",
            "policy_version": policy.version,
            "policy_sha256": policy.policy_sha256,
            "currency": None,
            "package_id": None,
            "base_price": None,
            "candidate_price": None,
            "owner_approval_required": True,
            "commercial_send_capability": False,
            "telegram_publication_capability": False,
            "ads_spend_capability": False,
        }

    request = session.get(AdRequest, ad_request_id)
    if request is None:
        raise ValueError("ad request does not exist")
    if request.payload.get("city") != "belgrade":
        raise ValueError("ad request is outside the configured city")

    package = policy.packages.get(package_id)
    if package is None:
        raise ValueError("unknown pricing package")
    if candidate_price is None or not candidate_price.is_finite() or candidate_price <= 0:
        raise ValueError("candidate_price must be an explicit positive decimal")

    latest_review = session.scalar(
        select(SuggestedPostReview)
        .where(SuggestedPostReview.ad_request_id == ad_request_id)
        .order_by(SuggestedPostReview.reviewed_at.desc(), SuggestedPostReview.id.desc())
        .limit(1)
    )
    if latest_review is None:
        status = "suggested_post_review_required"
        review_id: int | None = None
    elif latest_review.reviewed_payload_sha256 != _payload_hash(request.payload):
        status = "suggested_post_review_stale"
        review_id = latest_review.id
    elif latest_review.effective_outcome not in {
        "accept_candidate",
        "counter_offer_candidate",
    }:
        status = "suggested_post_not_priceable"
        review_id = latest_review.id
    else:
        status = "price_candidate_ready"
        review_id = latest_review.id

    within_bounds = (
        package.minimum_without_owner <= candidate_price <= package.maximum_without_owner
    )
    owner_approval_required = (
        policy.approval_mode == "owner_always"
        or not within_bounds
        or status != "price_candidate_ready"
    )

    return {
        "status": status,
        "policy_version": policy.version,
        "policy_sha256": policy.policy_sha256,
        "currency": policy.currency,
        "package_id": package.package_id,
        "base_price": str(package.base_price),
        "candidate_price": str(candidate_price),
        "minimum_without_owner": str(package.minimum_without_owner),
        "maximum_without_owner": str(package.maximum_without_owner),
        "within_owner_configured_bounds": within_bounds,
        "owner_approval_required": owner_approval_required,
        "suggested_post_review_id": review_id,
        "commercial_send_capability": False,
        "telegram_publication_capability": False,
        "ads_spend_capability": False,
    }
