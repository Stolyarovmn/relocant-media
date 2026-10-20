import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_channel_delivery import reviewed as _reviewed
from test_editorial_integration import prepare_service

from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.package import context_sha256, draft_sha256
from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.ingestion.persistence import capture_collected_batch
from relocant_media.ops import server_cycle
from relocant_media.services.editorial import DeliveryBlockedError
from relocant_media.storage.models import EditorialRun, RawItem, Source, SourceRevision
from relocant_media.storage.sqlite_schema import initialize_sqlite

reviewed = _reviewed


@pytest.fixture
def seeded(tmp_path):
    path = tmp_path / "editorial.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        _, provider, raw_id = prepare_service(session, category="transport")
    yield engine, raw_id, provider
    engine.dispose()


@pytest.mark.asyncio
async def test_publisher_fixed_target_and_prefix():
    calls = []

    class Bot:
        async def send_message(self, target, text):
            calls.append((target, text))
            return 12

    publisher = server_cycle.ChannelPublisher(Bot())
    with pytest.raises(ValueError, match="prefix"):
        await publisher.publish("Без маркировки")
    assert calls == []
    assert await publisher.publish("[тестовый запуск] Заголовок") == 12
    assert calls == [(-1003967014133, "[тестовый запуск] Заголовок")]


@pytest.mark.asyncio
async def test_disabled_delivery_needs_neither_engine_nor_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Бот и HTTP не должны создаваться")

    monkeypatch.setattr(server_cycle, "TelegramBotAPI", forbidden)
    monkeypatch.setattr(server_cycle.httpx, "AsyncClient", forbidden)
    assert await server_cycle.publish_pending(None, enabled=False) == []


def test_source_policy_prevents_ai_and_persists_hold(seeded, tmp_path):
    engine, raw_id, _ = seeded
    with Session(engine) as session:
        item = session.get(RawItem, raw_id)
        session.get(Source, item.source_id).auto_publish = False
        session.commit()

    class ForbiddenProvider:
        def generate_json(self, **kwargs):
            raise AssertionError("AI не должен вызываться")

    result = server_cycle.prepare_reviewed(engine, raw_id, ForbiddenProvider(), tmp_path)
    assert result["status"] == "human_review"
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        assert run.outcome["reason"] == "source_auto_publish_disabled"
        assert run.delivery_status == "pending"


def test_complete_package_is_bound_before_review(seeded, tmp_path, monkeypatch):
    engine, raw_id, provider = seeded
    seen = []

    def review(package, provider, live_documents, **kwargs):
        assert package.version == 2 and package.event is not None
        assert package.event.documents[0].raw_item_id == raw_id
        assert package.event.documents[0].ownership == "official"
        assert package.event.editorial_rule_version == "belgrade-editorial-v2"
        assert package.outcome.draft.title.startswith("[тестовый запуск]")
        digest = context_sha256(package)
        seen.append(digest)
        reviews = tuple(r.model_copy(update={
            "context_sha256": digest,
            "draft_sha256": draft_sha256(package.outcome.draft),
            "reviewed_at": datetime.now(UTC), "verdict": "approve", "score": 94,
            "flags": (), "live_source_checked": True,
        }) for r in package.reviews)
        return package.model_copy(update={"reviews": reviews})

    monkeypatch.setattr(server_cycle, "reviewed_package", review)
    assert server_cycle.prepare_reviewed(engine, raw_id, provider, tmp_path)["status"] == "ready"
    with Session(engine) as session:
        payload = session.get(EditorialRun, raw_id).outcome["_package"]
    package = server_cycle.EditorialPackage.model_validate_json(json.dumps(payload))
    assert context_sha256(package) == seen[0]
    assert all(r.context_sha256 == seen[0] for r in package.reviews)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["expired", "live_changed"])
async def test_invalid_ready_never_sends(reviewed, monkeypatch, mutation):
    session, run = reviewed
    if mutation == "expired":
        payload = dict(run.outcome)
        package = dict(payload["_package"])
        package["reviews"] = [{**r, "reviewed_at":
            (datetime.now(UTC) - timedelta(days=2)).isoformat()} for r in package["reviews"]]
        payload["_package"] = package
        run.outcome = payload
        session.commit()
    package = server_cycle.EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))
    docs = tuple(EvidenceDocument.model_validate(d.model_dump()) for d in package.event.documents)
    if mutation == "live_changed":
        docs = (docs[0].model_copy(update={"body": docs[0].body + " изменение"}),)
    monkeypatch.setattr(server_cycle, "fetch_documents", lambda package: docs)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    calls = []

    class Bot:
        def __init__(self, *args):
            pass

        async def send_message(self, target, text):
            calls.append((target, text))
            return 42

    monkeypatch.setattr(server_cycle, "TelegramBotAPI", Bot)
    with pytest.raises((ValueError, DeliveryBlockedError)):
        await server_cycle.publish_pending(session.get_bind(), enabled=True)
    assert calls == []
    session.refresh(run)
    assert run.delivery_status == "pending"


def test_revisions_preserve_original_and_each_new_version(reviewed):
    session, run = reviewed
    item = session.get(RawItem, run.raw_item_id)
    original = item.body
    for body in (original + " версия 2", original + " версия 3"):
        changed = CollectedItem(item.source_id, item.external_id, item.url, item.title,
                                body, item.published_at)
        capture_collected_batch(session, [changed])
        session.commit()
    session.refresh(item)
    assert item.body == original
    revisions = list(session.scalars(select(SourceRevision).order_by(SourceRevision.id)))
    assert len(revisions) == 2
    assert [r.evidence["body"] for r in revisions] == [
        original + " версия 2", original + " версия 3",
    ]


@pytest.mark.parametrize("status", ["PRIMARY_FAILED", "PRIMARY_BUSY"])
def test_failed_or_busy_main_never_enters_publisher(seeded, tmp_path, monkeypatch, status):
    engine, _, _ = seeded
    output = tmp_path / "run"
    monkeypatch.setenv("DATABASE_URL", str(engine.url))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setenv("GEMINI_MODEL", "fake-model")
    monkeypatch.setenv("EDITORIAL_SEND_ENABLED", "true")
    monkeypatch.setattr(sys, "argv", ["server-cycle", "--output", str(output)])
    clients = []

    def client(**kwargs):
        clients.append(kwargs)
        return object()

    monkeypatch.setattr(server_cycle.genai, "Client", client)
    monkeypatch.setattr(server_cycle, "GeminiStructuredProvider", lambda *a, **kw: object())

    def cycle(*args, **kwargs):
        assert kwargs["limit"] == 2
        output.mkdir()
        return {"status": status, "window_complete": False}

    monkeypatch.setattr(server_cycle, "run_cycle", cycle)

    def forbidden(*args, **kwargs):
        raise AssertionError("Отправка запрещена после неуспешного или занятого цикла")

    monkeypatch.setattr(server_cycle, "publish_pending", forbidden)
    with pytest.raises(SystemExit) as stopped:
        server_cycle.main()
    assert stopped.value.code == 1
    assert clients == [{"api_key": "fake-key", "http_options": {"timeout": 120000}}]
    report = json.loads((output / "server-report.json").read_text(encoding="utf-8"))
    assert report["status"] == status
    assert report["sent_message_ids"] == []
    assert report["channel_transport"] == "disabled"
    assert report["backup_ready"] is True
    assert (output / "snapshot" / "editorial.sqlite3").is_file()
    assert (output / "snapshot" / "manifest.json").is_file()
