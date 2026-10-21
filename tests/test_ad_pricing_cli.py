import json
import sys
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from relocant_media.ops import ad_pricing_cli
from relocant_media.services.ad_review import record_suggested_post_review
from relocant_media.storage.models import AdRequest, Base


def _database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        request = AdRequest(
            telegram_user_id=9,
            payload={"city": "belgrade", "text": "private"},
            status="new",
        )
        session.add(request)
        session.flush()
        record_suggested_post_review(
            session,
            ad_request_id=request.id,
            proposed_outcome="accept_candidate",
            reason_codes=["safe"],
            risk_flags=[],
            audience_fit=True,
            claims_verified=True,
            reviewer="test",
            idempotency_key="cli-review",
        )
        session.commit()
        request_id = request.id
    return engine, factory, request_id


def test_cli_unconfigured_policy_emits_no_money(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy = tmp_path / "pricing.yaml"
    policy.write_text(
        """version: ad-pricing-v1
configured: false
currency: null
packages: {}
approval:
  mode: owner_always
""",
        encoding="utf-8",
    )
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(ad_pricing_cli, "SessionFactory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-ad-price",
            "--policy",
            str(policy),
            "--ad-request-id",
            "999",
            "--package-id",
            "unset",
            "--candidate-price",
            "5000",
        ],
    )
    ad_pricing_cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "owner_configuration_required"
    assert result["currency"] is None
    assert result["candidate_price"] is None
    assert result["commercial_send_capability"] is False
    engine.dispose()


def test_cli_configured_policy_evaluates_only_candidate(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy = tmp_path / "pricing.yaml"
    policy.write_text(
        """version: owner-test-v1
configured: true
currency: EUR
packages:
  channel_post:
    base_price: '100.00'
    minimum_without_owner: '90.00'
    maximum_without_owner: '120.00'
approval:
  mode: bounded
""",
        encoding="utf-8",
    )
    engine, factory, request_id = _database()
    monkeypatch.setattr(ad_pricing_cli, "SessionFactory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-ad-price",
            "--policy",
            str(policy),
            "--ad-request-id",
            str(request_id),
            "--package-id",
            "channel_post",
            "--candidate-price",
            str(Decimal("110.00")),
        ],
    )
    ad_pricing_cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "price_candidate_ready"
    assert result["candidate_price"] == "110.00"
    assert result["owner_approval_required"] is False
    assert result["commercial_send_capability"] is False
    assert result["telegram_publication_capability"] is False
    assert result["ads_spend_capability"] is False
    engine.dispose()
