from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from relocant_media.services.correction_control import (
    build_reader_correction_control,
    record_reader_correction_decision,
)
from relocant_media.storage.correction_control import ReaderCorrectionDecision
from relocant_media.storage.models import Base, Submission


def _correction(
    session: Session,
    *,
    message_id: int,
    created_at: datetime,
    status: str = "new",
) -> Submission:
    row = Submission(
        kind="correction",
        telegram_user_id=1000 + message_id,
        telegram_message_id=message_id,
        status=status,
        created_at=created_at,
        payload={
            "city": "belgrade",
            "chat_id": 1000 + message_id,
            "text": f"private correction {message_id}",
            "raw_item_ids": [message_id],
        },
    )
    session.add(row)
    session.flush()
    return row


def test_decision_is_append_only_idempotent_and_does_not_mutate_submission() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        row = _correction(
            session,
            message_id=7,
            status="source_linked",
            created_at=datetime(2026, 10, 21, 12, tzinfo=UTC),
        )
        first = record_reader_correction_decision(
            session,
            submission_id=row.id,
            decision="confirmed",
            reason_code="official_source_confirms_error",
            actor="editorial-agent",
            idempotency_key="correction-7-confirmed",
            note="Audited conclusion only",
            decided_at=datetime(2026, 10, 21, 12, 5, tzinfo=UTC),
        )
        repeated = record_reader_correction_decision(
            session,
            submission_id=row.id,
            decision="confirmed",
            reason_code="official_source_confirms_error",
            actor="editorial-agent",
            idempotency_key="correction-7-confirmed",
            note="Audited conclusion only",
            decided_at=datetime(2026, 10, 21, 12, 10, tzinfo=UTC),
        )
        assert repeated.id == first.id
        assert row.status == "source_linked"
        assert session.scalar(select(func.count()).select_from(ReaderCorrectionDecision)) == 1

        report = build_reader_correction_control(session)
        item = report["items"][0]
        assert item["decision_status"] == "resolved"
        assert item["effective_decision"] == "confirmed"
        assert item["telegram_mutation_performed"] is False
        assert item["private_text_exposed"] is False
        assert "private correction" not in str(report)
        assert str(row.telegram_user_id) not in str(report)
    engine.dispose()


def test_only_correction_submissions_can_receive_decisions() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        row = Submission(
            kind="news_tip",
            telegram_user_id=1,
            telegram_message_id=1,
            payload={"city": "belgrade", "text": "tip"},
            status="new",
        )
        session.add(row)
        session.flush()
        with pytest.raises(ValueError, match="not a reader correction"):
            record_reader_correction_decision(
                session,
                submission_id=row.id,
                decision="hold",
                reason_code="not_applicable",
                actor="operator",
                idempotency_key="tip-is-not-correction",
            )
    engine.dispose()


def test_unsafe_publication_decision_is_rejected() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        row = _correction(
            session,
            message_id=8,
            created_at=datetime(2026, 10, 21, 13, tzinfo=UTC),
        )
        with pytest.raises(ValueError, match="unsupported"):
            record_reader_correction_decision(
                session,
                submission_id=row.id,
                decision="edit_telegram_now",
                reason_code="unsafe",
                actor="operator",
                idempotency_key="unsafe-edit",
            )
    engine.dispose()


def test_idempotency_key_cannot_be_reused_for_another_decision() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        row = _correction(
            session,
            message_id=9,
            created_at=datetime(2026, 10, 21, 14, tzinfo=UTC),
        )
        record_reader_correction_decision(
            session,
            submission_id=row.id,
            decision="hold",
            reason_code="needs_evidence",
            actor="operator",
            idempotency_key="same-key",
        )
        with pytest.raises(ValueError, match="another correction decision"):
            record_reader_correction_decision(
                session,
                submission_id=row.id,
                decision="not_supported",
                reason_code="no_public_evidence",
                actor="operator",
                idempotency_key="same-key",
            )
    engine.dispose()


def test_control_projection_prioritizes_unresolved_and_never_exposes_private_text() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 21, 15, tzinfo=UTC)
    with Session(engine) as session:
        older = _correction(session, message_id=10, created_at=now - timedelta(hours=1))
        newer = _correction(session, message_id=11, created_at=now)
        record_reader_correction_decision(
            session,
            submission_id=newer.id,
            decision="not_supported",
            reason_code="no_public_evidence",
            actor="editorial-agent",
            idempotency_key="newer-resolved",
        )
        report = build_reader_correction_control(session)
        assert report["unresolved_corrections"] == 1
        assert report["items"][0]["submission_id"] == older.id
        assert report["items"][0]["effective_decision"] == "unreviewed"
        assert report["items"][1]["submission_id"] == newer.id
        assert report["items"][1]["decision_status"] == "resolved"
        assert report["safety"]["telegram_edit_capability"] is False
        assert "private correction" not in str(report)
    engine.dispose()
