import json
import sys

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from relocant_media.ops import correction_control_cli
from relocant_media.storage.correction_control import ReaderCorrectionDecision
from relocant_media.storage.models import Base, Submission


def _database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        row = Submission(
            kind="correction",
            telegram_user_id=100,
            telegram_message_id=200,
            payload={"city": "belgrade", "text": "private correction"},
            status="new",
        )
        session.add(row)
        session.commit()
        submission_id = row.id
    return engine, factory, submission_id


def test_cli_requires_explicit_write_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory, submission_id = _database()
    monkeypatch.setattr(correction_control_cli, "SessionFactory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-correction-control",
            "--submission-id",
            str(submission_id),
            "--decision",
            "hold",
            "--reason-code",
            "needs_evidence",
            "--idempotency-key",
            "cli-test",
            "--expected-decision-count",
            "0",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        correction_control_cli.main()
    assert exc.value.code == 2
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ReaderCorrectionDecision)) == 0
    engine.dispose()


def test_cli_records_audit_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, factory, submission_id = _database()
    monkeypatch.setattr(correction_control_cli, "SessionFactory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-correction-control",
            "--submission-id",
            str(submission_id),
            "--decision",
            "confirmed",
            "--reason-code",
            "verified_error",
            "--actor",
            "test",
            "--idempotency-key",
            "cli-confirmed",
            "--expected-decision-count",
            "0",
            "--confirm-write",
        ],
    )
    correction_control_cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["submission_id"] == submission_id
    assert payload["decision"] == "confirmed"
    assert payload["telegram_edit_performed"] is False
    assert payload["automatic_publication"] is False
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ReaderCorrectionDecision)) == 1
        row = session.get(Submission, submission_id)
        assert row is not None and row.status == "new"
    engine.dispose()


def test_cli_rejects_stale_expected_decision_count(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, factory, submission_id = _database()
    monkeypatch.setattr(correction_control_cli, "SessionFactory", factory)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-correction-control",
            "--submission-id",
            str(submission_id),
            "--decision",
            "hold",
            "--reason-code",
            "first",
            "--idempotency-key",
            "first-write",
            "--expected-decision-count",
            "0",
            "--confirm-write",
        ],
    )
    correction_control_cli.main()
    capsys.readouterr()

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-correction-control",
            "--submission-id",
            str(submission_id),
            "--decision",
            "confirmed",
            "--reason-code",
            "stale",
            "--idempotency-key",
            "stale-write",
            "--expected-decision-count",
            "0",
            "--confirm-write",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        correction_control_cli.main()
    assert exc.value.code == 1
    assert "stale reader correction control snapshot" in capsys.readouterr().err
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(ReaderCorrectionDecision)) == 1
    engine.dispose()
