from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from relocant_media.services.ad_review import (
    build_suggested_post_review_control,
    record_suggested_post_review,
)
from relocant_media.storage.ad_review import SuggestedPostReview
from relocant_media.storage.models import AdRequest, Base


def _request(session: Session, *, text: str = "Реклама локального кафе") -> AdRequest:
    row = AdRequest(
        telegram_user_id=987654321,
        payload={
            "city": "belgrade",
            "chat_id": 123456,
            "text": text,
            "contact": "private@example.test",
        },
        status="new",
        created_at=datetime(2026, 10, 21, 16, tzinfo=UTC),
    )
    session.add(row)
    session.flush()
    return row


def test_safe_accept_remains_candidate_and_never_becomes_publication_approval() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        request = _request(session)
        review = record_suggested_post_review(
            session,
            ad_request_id=request.id,
            proposed_outcome="accept_candidate",
            reason_codes=["local_service_relevant"],
            risk_flags=[],
            audience_fit=True,
            claims_verified=True,
            reviewer="ad-review-agent",
            idempotency_key="ad-1-safe",
        )
        assert review.effective_outcome == "accept_candidate"
        assert review.requires_owner_approval is True
        assert request.status == "new"

        control = build_suggested_post_review_control(session)
        item = control["items"][0]
        assert item["review_status"] == "candidate_reviewed"
        assert item["publication_capability"] is False
        assert item["spend_capability"] is False
        assert control["safety"]["accept_candidate_is_publication_approval"] is False
        assert control["safety"]["owner_approval_required_for_publication"] is True
    engine.dispose()


@pytest.mark.parametrize(
    "flag",
    ["medicine", "investment", "credit", "gambling", "political", "unverifiable_earnings"],
)
def test_restricted_category_forces_human_review(flag: str) -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        request = _request(session)
        review = record_suggested_post_review(
            session,
            ad_request_id=request.id,
            proposed_outcome="accept_candidate",
            reason_codes=["reviewer_proposed_accept"],
            risk_flags=[flag],
            audience_fit=True,
            claims_verified=True,
            reviewer="ad-review-agent",
            idempotency_key=f"restricted-{flag}",
        )
        assert review.effective_outcome == "human_review"
        assert "restricted_category_requires_human_review" in review.reason_codes
    engine.dispose()


def test_hard_reject_flag_overrides_more_permissive_proposal() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        request = _request(session)
        review = record_suggested_post_review(
            session,
            ad_request_id=request.id,
            proposed_outcome="counter_offer_candidate",
            reason_codes=["could_be_reworked"],
            risk_flags=["scam"],
            audience_fit=True,
            claims_verified=True,
            reviewer="ad-review-agent",
            idempotency_key="hard-reject",
        )
        assert review.effective_outcome == "reject"
        assert "hard_reject_category" in review.reason_codes
    engine.dispose()


def test_unverified_or_mismatched_accept_is_forced_to_human_review() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        first_request = _request(session, text="Первый текст")
        first = record_suggested_post_review(
            session,
            ad_request_id=first_request.id,
            proposed_outcome="accept_candidate",
            reason_codes=["proposal"],
            risk_flags=[],
            audience_fit=False,
            claims_verified=True,
            reviewer="agent",
            idempotency_key="audience-mismatch",
        )
        assert first.effective_outcome == "human_review"
        assert "audience_fit_not_confirmed" in first.reason_codes

        second_request = _request(session, text="Второй текст")
        second = record_suggested_post_review(
            session,
            ad_request_id=second_request.id,
            proposed_outcome="accept_candidate",
            reason_codes=["proposal"],
            risk_flags=[],
            audience_fit=True,
            claims_verified=False,
            reviewer="agent",
            idempotency_key="claims-unverified",
        )
        assert second.effective_outcome == "human_review"
        assert "claims_not_verified" in second.reason_codes
    engine.dispose()


def test_review_is_idempotent_and_payload_change_makes_it_stale() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        request = _request(session)
        first = record_suggested_post_review(
            session,
            ad_request_id=request.id,
            proposed_outcome="human_review",
            reason_codes=["needs_manual_check"],
            risk_flags=[],
            audience_fit=True,
            claims_verified=False,
            reviewer="operator",
            idempotency_key="same-review",
            reviewer_metadata={"source": "manual"},
        )
        repeated = record_suggested_post_review(
            session,
            ad_request_id=request.id,
            proposed_outcome="human_review",
            reason_codes=["needs_manual_check"],
            risk_flags=[],
            audience_fit=True,
            claims_verified=False,
            reviewer="operator",
            idempotency_key="same-review",
            reviewer_metadata={"source": "manual"},
        )
        assert repeated.id == first.id
        assert session.scalar(select(func.count()).select_from(SuggestedPostReview)) == 1

        request.payload = {**request.payload, "text": "Изменённый приватный рекламный текст"}
        session.flush()
        control = build_suggested_post_review_control(session)
        item = control["items"][0]
        assert item["review_status"] == "stale"
        assert item["payload_changed_after_review"] is True
        assert "Изменённый" not in str(control)
        assert "private@example.test" not in str(control)
        assert str(request.telegram_user_id) not in str(control)
    engine.dispose()


def test_idempotency_key_cannot_be_reused_for_different_review() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        request = _request(session)
        record_suggested_post_review(
            session,
            ad_request_id=request.id,
            proposed_outcome="human_review",
            reason_codes=["first"],
            risk_flags=[],
            audience_fit=True,
            claims_verified=False,
            reviewer="operator",
            idempotency_key="review-key",
        )
        with pytest.raises(ValueError, match="another suggested-post review"):
            record_suggested_post_review(
                session,
                ad_request_id=request.id,
                proposed_outcome="reject",
                reason_codes=["different"],
                risk_flags=["scam"],
                audience_fit=False,
                claims_verified=False,
                reviewer="operator",
                idempotency_key="review-key",
            )
    engine.dispose()
