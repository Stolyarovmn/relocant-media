import json
import sys

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from relocant_media.ops import ad_review_cli
from relocant_media.storage.ad_review import SuggestedPostReview
from relocant_media.storage.models import AdRequest, Base


def _database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        request = AdRequest(
            telegram_user_id=77,
            payload={"city": "belgrade", "text": "private ad text"},
            status="new",
        )
        session.add(request)
        session.commit()
        request_id = request.id
    return engine, factory, request_id


def test_cli_requires_explicit_write_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory, request_id = _database()
    monkeypatch.setattr(ad_review_cli, "SessionFactory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-ad-review",
            "--ad-request-id",
            str(request_id),
            "--proposed-outcome",
            "human_review",
            "--reason-code",
            "manual_check",
            "--audience-fit",
            "yes",
            "--claims-verified",
            "no",
            "--idempotency-key",
            "cli-no-confirm",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        ad_review_cli.main()
    assert exc.value.code == 2
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SuggestedPostReview)) == 0
    engine.dispose()


def test_cli_records_candidate_review_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, factory, request_id = _database()
    monkeypatch.setattr(ad_review_cli, "SessionFactory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-ad-review",
            "--ad-request-id",
            str(request_id),
            "--proposed-outcome",
            "accept_candidate",
            "--reason-code",
            "local_relevance",
            "--audience-fit",
            "yes",
            "--claims-verified",
            "yes",
            "--reviewer",
            "test",
            "--idempotency-key",
            "cli-confirmed",
            "--confirm-write",
        ],
    )
    ad_review_cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["ad_request_id"] == request_id
    assert payload["effective_outcome"] == "accept_candidate"
    assert payload["requires_owner_approval"] is True
    assert payload["telegram_publication_performed"] is False
    assert payload["ads_spend_performed"] is False
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SuggestedPostReview)) == 1
        request = session.get(AdRequest, request_id)
        assert request is not None and request.status == "new"
    engine.dispose()


def test_cli_restricted_flag_cannot_auto_accept(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, factory, request_id = _database()
    monkeypatch.setattr(ad_review_cli, "SessionFactory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-ad-review",
            "--ad-request-id",
            str(request_id),
            "--proposed-outcome",
            "accept_candidate",
            "--reason-code",
            "model_proposed_accept",
            "--risk-flag",
            "investment",
            "--audience-fit",
            "yes",
            "--claims-verified",
            "yes",
            "--idempotency-key",
            "cli-restricted",
            "--confirm-write",
        ],
    )
    ad_review_cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["effective_outcome"] == "human_review"
    assert "restricted_category_requires_human_review" in payload["reason_codes"]
    engine.dispose()
