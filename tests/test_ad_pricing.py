from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.services.ad_pricing import (
    evaluate_ad_price_candidate,
    load_ad_pricing_policy,
)
from relocant_media.services.ad_review import record_suggested_post_review
from relocant_media.storage.models import AdRequest, Base


def _write_policy(tmp_path, text: str):
    path = tmp_path / "ad-pricing.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _configured_policy(tmp_path, *, mode: str = "bounded"):
    return load_ad_pricing_policy(
        _write_policy(
            tmp_path,
            f"""version: test-pricing-v1
configured: true
currency: EUR
packages:
  channel_post:
    base_price: '100.00'
    minimum_without_owner: '90.00'
    maximum_without_owner: '120.00'
approval:
  mode: {mode}
""",
        )
    )


def _request(session: Session) -> AdRequest:
    request = AdRequest(
        telegram_user_id=123,
        payload={
            "city": "belgrade",
            "text": "private advertiser text",
            "contact": "private@example.test",
        },
        status="new",
    )
    session.add(request)
    session.flush()
    return request


def _approve_candidate(session: Session, request: AdRequest) -> None:
    record_suggested_post_review(
        session,
        ad_request_id=request.id,
        proposed_outcome="accept_candidate",
        reason_codes=["test_safe_candidate"],
        risk_flags=[],
        audience_fit=True,
        claims_verified=True,
        reviewer="test",
        idempotency_key=f"review-{request.id}",
    )


def test_unconfigured_policy_contains_no_money_and_fails_closed(tmp_path) -> None:
    policy = load_ad_pricing_policy(
        _write_policy(
            tmp_path,
            """version: ad-pricing-v1
configured: false
currency: null
packages: {}
approval:
  mode: owner_always
""",
        )
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        result = evaluate_ad_price_candidate(
            session,
            policy=policy,
            ad_request_id=999,
            package_id="not-configured",
            candidate_price=Decimal("999999"),
        )
    assert result["status"] == "owner_configuration_required"
    assert result["currency"] is None
    assert result["package_id"] is None
    assert result["base_price"] is None
    assert result["candidate_price"] is None
    assert result["owner_approval_required"] is True
    assert result["commercial_send_capability"] is False
    engine.dispose()


def test_unconfigured_policy_cannot_hide_partial_money_configuration(tmp_path) -> None:
    with pytest.raises(ValueError, match="unconfigured pricing"):
        load_ad_pricing_policy(
            _write_policy(
                tmp_path,
                """version: ad-pricing-v1
configured: false
currency: EUR
packages: {}
approval:
  mode: owner_always
""",
            )
        )


def test_configured_policy_requires_explicit_complete_bounds(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires base/minimum/maximum"):
        load_ad_pricing_policy(
            _write_policy(
                tmp_path,
                """version: test
configured: true
currency: EUR
packages:
  channel_post:
    base_price: '100'
approval:
  mode: bounded
""",
            )
        )


def test_bounded_policy_allows_candidate_inside_owner_configured_bounds(tmp_path) -> None:
    policy = _configured_policy(tmp_path)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        request = _request(session)
        _approve_candidate(session, request)
        result = evaluate_ad_price_candidate(
            session,
            policy=policy,
            ad_request_id=request.id,
            package_id="channel_post",
            candidate_price=Decimal("95.00"),
        )
        assert result["status"] == "price_candidate_ready"
        assert result["currency"] == "EUR"
        assert result["base_price"] == "100.00"
        assert result["candidate_price"] == "95.00"
        assert result["within_owner_configured_bounds"] is True
        assert result["owner_approval_required"] is False
        assert result["commercial_send_capability"] is False
        assert result["telegram_publication_capability"] is False
        assert result["ads_spend_capability"] is False
        assert "private advertiser text" not in str(result)
        assert "private@example.test" not in str(result)
    engine.dispose()


def test_outside_bounds_or_owner_always_requires_owner_approval(tmp_path) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        request = _request(session)
        _approve_candidate(session, request)
        bounded = evaluate_ad_price_candidate(
            session,
            policy=_configured_policy(tmp_path, mode="bounded"),
            ad_request_id=request.id,
            package_id="channel_post",
            candidate_price=Decimal("80.00"),
        )
        assert bounded["owner_approval_required"] is True
        assert bounded["within_owner_configured_bounds"] is False

        owner_always = evaluate_ad_price_candidate(
            session,
            policy=_configured_policy(tmp_path, mode="owner_always"),
            ad_request_id=request.id,
            package_id="channel_post",
            candidate_price=Decimal("100.00"),
        )
        assert owner_always["within_owner_configured_bounds"] is True
        assert owner_always["owner_approval_required"] is True
    engine.dispose()


def test_missing_stale_or_non_priceable_review_blocks_candidate(tmp_path) -> None:
    policy = _configured_policy(tmp_path)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        missing = _request(session)
        result = evaluate_ad_price_candidate(
            session,
            policy=policy,
            ad_request_id=missing.id,
            package_id="channel_post",
            candidate_price=Decimal("100"),
        )
        assert result["status"] == "suggested_post_review_required"
        assert result["owner_approval_required"] is True

        stale = _request(session)
        _approve_candidate(session, stale)
        stale.payload = {**stale.payload, "text": "changed private text"}
        session.flush()
        result = evaluate_ad_price_candidate(
            session,
            policy=policy,
            ad_request_id=stale.id,
            package_id="channel_post",
            candidate_price=Decimal("100"),
        )
        assert result["status"] == "suggested_post_review_stale"
        assert result["owner_approval_required"] is True

        blocked = _request(session)
        record_suggested_post_review(
            session,
            ad_request_id=blocked.id,
            proposed_outcome="accept_candidate",
            reason_codes=["restricted"],
            risk_flags=["investment"],
            audience_fit=True,
            claims_verified=True,
            reviewer="test",
            idempotency_key=f"restricted-{blocked.id}",
        )
        result = evaluate_ad_price_candidate(
            session,
            policy=policy,
            ad_request_id=blocked.id,
            package_id="channel_post",
            candidate_price=Decimal("100"),
        )
        assert result["status"] == "suggested_post_not_priceable"
        assert result["owner_approval_required"] is True
    engine.dispose()
