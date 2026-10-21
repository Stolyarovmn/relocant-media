import json
import sys
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from relocant_media.ops import revision_control_cli
from relocant_media.storage.models import Base, RawItem, Source, SourceRevision
from relocant_media.storage.revision_control import (
    SourceRevisionDecision,
    SourceRevisionMaterialization,
)


def _database():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        source = Source(
            id="official",
            city="belgrade",
            name="Official",
            url="https://example.test",
            source_type="official",
            trust=Decimal("1"),
        )
        session.add(source)
        session.flush()
        raw = RawItem(
            source_id=source.id,
            external_id="1",
            url="https://example.test/1",
            title="Title",
            body="Body",
        )
        session.add(raw)
        session.flush()
        revision = SourceRevision(
            raw_item_id=raw.id,
            snapshot_sha256="a" * 64,
            evidence={
                "source_id": source.id,
                "article_id": raw.external_id,
                "url": raw.url,
                "title": "Changed title",
                "body": "Changed",
                "published_at": datetime(2026, 10, 21, 12, tzinfo=UTC).isoformat(),
            },
        )
        session.add(revision)
        session.commit()
        revision_id = revision.id
    return engine, factory, revision_id


def test_cli_requires_explicit_write_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, factory, revision_id = _database()
    monkeypatch.setattr(revision_control_cli, "SessionFactory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-revision-control",
            "--revision-id",
            str(revision_id),
            "--decision",
            "hold",
            "--reason-code",
            "needs_context",
            "--idempotency-key",
            "cli-test",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        revision_control_cli.main()
    assert exc.value.code == 2
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SourceRevisionDecision)) == 0
        assert session.scalar(select(func.count()).select_from(SourceRevisionMaterialization)) == 0
    engine.dispose()


def test_cli_queues_fresh_candidate_without_approval_or_telegram(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    engine, factory, revision_id = _database()
    monkeypatch.setattr(revision_control_cli, "SessionFactory", factory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "relocant-revision-control",
            "--revision-id",
            str(revision_id),
            "--decision",
            "queue_reverify",
            "--reason-code",
            "material_change",
            "--actor",
            "test",
            "--idempotency-key",
            "cli-confirmed",
            "--confirm-write",
        ],
    )
    revision_control_cli.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["revision_id"] == revision_id
    assert payload["decision"] == "queue_reverify"
    assert payload["fresh_review_cleared"] is False
    assert payload["telegram_side_effect"] is False
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(SourceRevisionDecision)) == 1
        assert session.scalar(select(func.count()).select_from(SourceRevisionMaterialization)) == 1
        assert session.scalar(select(func.count()).select_from(RawItem)) == 2
    engine.dispose()
