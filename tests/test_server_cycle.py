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
from relocant_media.domain.freshness import FreshnessDecision
from relocant_media.ingestion.persistence import capture_collected_batch
from relocant_media.ops import server_cycle
from relocant_media.storage.models import EditorialRun, RawItem, Source, SourceRevision
from relocant_media.storage.sqlite_schema import initialize_sqlite

reviewed = _reviewed


def test_configured_limit_enters_real_cycle_without_ai_or_telegram(tmp_path):
    from relocant_media.ops.primary_cycle import run_cycle

    path = tmp_path / "editorial.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    try:
        result = run_cycle(
            engine, city="belgrade", output=tmp_path / "cycle", now=datetime.now(UTC),
            limit=server_cycle.PROCESS_LIMIT, ingest=lambda: {"failures": 0},
            process=lambda raw_id: pytest.fail("empty queue must not call AI or Telegram"),
        )
        assert result["status"] == "PRIMARY_OK"
    finally:
        engine.dispose()


def test_server_roles_receive_russian_policy_without_rewriting_evidence():
    calls = []

    class Provider:
        def generate_json(self, **kwargs):
            calls.append(kwargs)
            return '{"result":"готово"}'

    result = server_cycle.ServerAI(Provider()).generate_json(
        task="rank", system_instruction="Исходная инструкция", prompt='{"evidence":"точно"}',
        response_schema={"type": "object"})
    assert result == '{"result":"готово"}'
    assert calls[0]["prompt"] == '{"evidence":"точно"}'
    assert calls[0]["response_schema"] == {"type": "object"}
    assert "по-русски" in calls[0]["system_instruction"]
    assert "Текущий момент UTC:" in calls[0]["system_instruction"]
    assert calls[0]["system_instruction"].endswith("Исходная инструкция")


@pytest.fixture
def seeded(tmp_path):
    path = tmp_path / "editorial.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        _, provider, raw_id = prepare_service(session, category="transport")
        session.get(RawItem, raw_id).published_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()
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
    with pytest.raises(ValueError, match="presentation"):
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


def test_old_source_is_discarded_without_ai(seeded, tmp_path):
    engine, raw_id, _ = seeded
    with Session(engine) as session:
        item = session.get(RawItem, raw_id)
        item.published_at = datetime.now(UTC) - timedelta(days=10)
        session.get(Source, item.source_id).auto_publish = False
        session.commit()

    class ForbiddenProvider:
        def generate_json(self, **kwargs):
            raise AssertionError("Устаревшая статья не должна тратить AI")

    result = server_cycle.prepare_reviewed(engine, raw_id, ForbiddenProvider(), tmp_path)
    assert result["status"] == "skipped"
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        assert run.outcome["reason"] == "publication_older_than_seven_days"
        assert run.post_id is None


def test_duplicate_of_a_reviewed_event_is_closed_once_instead_of_retried_every_cycle(
        seeded, tmp_path):
    engine, raw_id, _ = seeded
    with Session(engine) as session:
        first = session.get(RawItem, raw_id)
        session.add(EditorialRun(raw_item_id=first.id, outcome={"status": "skipped"},
                                 evidence={}, delivery_status="pending"))
        twin = RawItem(source_id=first.source_id, external_id="twin-of-first", url=first.url,
                       title=first.title, body=first.body, published_at=first.published_at)
        session.add(twin)
        session.commit()
        twin_id = twin.id

    class ForbiddenProvider:
        def generate_json(self, **kwargs):
            raise AssertionError("A duplicate must not cost a model call")

    result = server_cycle.prepare_reviewed(engine, twin_id, ForbiddenProvider(), tmp_path)
    assert result["status"] == "skipped" and result["reason"] == "event_already_reviewed"
    with Session(engine) as session:
        run = session.get(EditorialRun, twin_id)
        assert run.outcome["reason"] == "event_already_reviewed" and run.post_id is None
        assert run.outcome["duplicate_of_raw_item"] == raw_id
    # The next cycle sees an existing run and moves on without any error.
    again = server_cycle.prepare_reviewed(engine, twin_id, ForbiddenProvider(), tmp_path)
    assert again["reason"] == "event_already_reviewed"


@pytest.mark.parametrize("source_enabled", [True, False])
def test_complete_package_is_bound_before_review(seeded, tmp_path, monkeypatch, source_enabled):
    engine, raw_id, provider = seeded
    seen = []
    with Session(engine) as session:
        item = session.get(RawItem, raw_id)
        session.get(Source, item.source_id).auto_publish = source_enabled
        session.commit()

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

    monkeypatch.setattr(server_cycle, "review_with_repairs", review)
    assert server_cycle.prepare_reviewed(engine, raw_id, provider, tmp_path)["status"] == (
        "ready" if source_enabled else "human_review")
    assert provider.calls == ["rank", "claims", "verify", "draft"]
    with Session(engine) as session:
        payload = session.get(EditorialRun, raw_id).outcome["_package"]
    package = server_cycle.EditorialPackage.model_validate_json(json.dumps(payload))
    assert context_sha256(package) == seen[0]
    assert all(r.context_sha256 == seen[0] for r in package.reviews)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["expired", "live_changed"])
async def test_invalid_ready_never_sends(reviewed, monkeypatch, mutation):
    session, run = reviewed
    monkeypatch.setattr(server_cycle, "freshness_decision",
                        lambda **kw: FreshnessDecision("keep", "test_current"))
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
    blocked = []
    assert await server_cycle.publish_pending(
        session.get_bind(), enabled=True, blocked=blocked) == []
    assert blocked[0]["raw_item_id"] == run.raw_item_id
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
    def cycle(*args, **kwargs):
        assert kwargs["limit"] == server_cycle.PROCESS_LIMIT == 10
        output.mkdir()
        return {"status": status, "window_complete": False}

    monkeypatch.setattr(server_cycle, "run_cycle", cycle)

    def forbidden(*args, **kwargs):
        raise AssertionError("Отправка запрещена после неуспешного или занятого цикла")

    monkeypatch.setattr(server_cycle, "publish_pending", forbidden)
    with pytest.raises(SystemExit) as stopped:
        server_cycle.main()
    assert stopped.value.code == 1
    report = json.loads((output / "server-report.json").read_text(encoding="utf-8"))
    assert report["status"] == status
    assert report["sent_message_ids"] == []
    assert report["channel_transport"] == "disabled"
    assert report["backup_ready"] is True
    assert (output / "snapshot" / "editorial.sqlite3").is_file()
    assert (output / "snapshot" / "manifest.json").is_file()


def test_triage_old_holds_closes_without_copying_body_or_touching_receipts(seeded):
    from relocant_media.ops.newsroom_triage import triage_queue
    engine, raw_id, _ = seeded
    with Session(engine) as session:
        item = session.get(RawItem, raw_id)
        item.published_at = datetime.now(UTC) - timedelta(days=10)
        session.add(EditorialRun(raw_item_id=raw_id,
            outcome={"status": "human_review", "reason": "source_auto_publish_disabled"},
            evidence={"original": "retained"}, delivery_status="pending"))
        session.commit()
    report = triage_queue(engine, now=datetime.now(UTC))
    assert report["discarded"] == 1
    assert report["ai_calls"] == 0 and "body" not in json.dumps(report)
    assert triage_queue(engine, now=datetime.now(UTC))["discarded"] == 0
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        assert run.outcome["status"] == "skipped"
        assert run.outcome["previous_decision"]["status"] == "human_review"
        assert run.evidence == {"original": "retained"}


@pytest.mark.parametrize("field", ["receipt", "draft", "verification", "package", "assessed"])
def test_cheap_triage_never_rewrites_review_or_receipt(seeded, field):
    from relocant_media.ops.newsroom_triage import triage_queue
    engine, raw_id, _ = seeded
    with Session(engine) as session:
        session.get(RawItem, raw_id).published_at = datetime.now(UTC) - timedelta(days=10)
        outcome = {"status": "human_review", "reason": "source_auto_publish_disabled"}
        keys = {"draft": "draft", "verification": "verification", "package": "_package",
                "assessed": "_autonomous_assessment"}
        if field in keys:
            outcome[keys[field]] = {}
        session.add(EditorialRun(raw_item_id=raw_id, outcome=outcome, evidence={},
            delivery_status="pending", staging_message_id=42 if field == "receipt" else None))
        session.commit()
    assert triage_queue(engine, now=datetime.now(UTC))["discarded"] == 0
    with Session(engine) as session:
        assert session.get(EditorialRun, raw_id).outcome == outcome


def test_rank_uses_machine_categories_while_reasons_stay_russian():
    from relocant_media.ai.models import RankingResult
    calls = []
    class Provider:
        def generate_json(self, **kw):
            calls.append(kw)
            return "{}"
    original = RankingResult.model_json_schema()
    server_cycle.ServerAI(Provider()).generate_json(task="rank", system_instruction="Ранжируй",
        prompt="публичный источник", response_schema=original)
    assert "category — машинный идентификатор" in calls[0]["system_instruction"]
    assert "events" in calls[0]["response_schema"]["properties"]["category"]["enum"]
    assert "medicine" in calls[0]["response_schema"]["properties"]["category"]["enum"]
    assert "enum" not in original["properties"]["category"]
    # Macro-economic and central-bank news must not be forced into the high-risk "banking".
    assert "economy" in calls[0]["response_schema"]["properties"]["category"]["enum"]
    assert "economy" in calls[0]["system_instruction"]



def test_runtime_venue_sources_include_dated_structured_collectors():
    venues = server_cycle.venue_sources()
    assert "venue_musicology" in venues
    assert venues["venue_musicology"].extraction.strategy == "musicology_blog"
    assert "venue_natural_history" in venues
    assert venues["venue_natural_history"].extraction.strategy == "natural_history_news"
    assert "venue_bitefartcafe" not in venues
    assert "venue_yugoslavia" not in venues

    review_only = server_cycle.review_only_venue_sources()
    assert set(review_only) == {"venue_bitefartcafe", "venue_yugoslavia"}
    assert all(not source.policy.auto_publish for source in review_only.values())

    discovery = server_cycle.discovery_venue_sources()
    assert discovery
    assert all(source.extraction.strategy == "venue_feed" for source in discovery.values())
    assert set(discovery) < set(venues)


@pytest.mark.asyncio
async def test_structured_venue_exact_refresh_requires_one_matching_official_url():
    target = CollectedItem(
        source_id="venue_musicology",
        external_id="https://musicology.rs/2026/10/14/example/",
        url="https://musicology.rs/2026/10/14/example/",
        title="Example",
        body="Verified official body",
        published_at=datetime(2026, 10, 14, 12, tzinfo=UTC),
        canonical_url="https://musicology.rs/2026/10/14/example/",
    )

    class StructuredCollector:
        async def collect(self):
            return [target]

    assert await server_cycle._collect_exact_url(
        StructuredCollector(), target.url
    ) is target

    class AmbiguousCollector:
        async def collect(self):
            return [target, target]

    with pytest.raises(ValueError, match="unavailable or ambiguous"):
        await server_cycle._collect_exact_url(AmbiguousCollector(), target.url)



def test_review_only_venue_hold_requires_future_event_and_unknown_publication():
    now = datetime(2026, 10, 22, 12, tzinfo=UTC)
    future = CollectedItem(
        source_id="venue_bitefartcafe",
        external_id="https://bitefartcafe.rs/dogadjaji/example/",
        url="https://bitefartcafe.rs/dogadjaji/example/",
        title="Koncert 30.10.2026.",
        body="Koncert će se održati 30.10.2026. u Beogradu. Ulaznice su dostupne.",
        published_at=None,
    )
    assert server_cycle._is_review_only_venue_hold(future, now=now)

    expired = future.__class__(
        source_id=future.source_id,
        external_id=future.external_id + "expired",
        url=future.url + "expired",
        title="Koncert 20.10.2026.",
        body="Koncert je održan 20.10.2026. u Beogradu. Ulaznice su bile dostupne.",
        published_at=None,
    )
    assert not server_cycle._is_review_only_venue_hold(expired, now=now)

    dated = future.__class__(
        source_id=future.source_id,
        external_id=future.external_id + "dated",
        url=future.url + "dated",
        title=future.title,
        body=future.body,
        published_at=now - timedelta(hours=1),
    )
    assert not server_cycle._is_review_only_venue_hold(dated, now=now)


def test_unknown_publication_date_triages_to_human_review_without_ai(seeded):
    from relocant_media.ops.newsroom_triage import triage_queue

    engine, raw_id, _ = seeded
    with Session(engine) as session:
        item = session.get(RawItem, raw_id)
        item.published_at = None
        session.commit()

    report = triage_queue(engine, now=datetime.now(UTC))
    assert report["metadata_deferred"] == 1
    assert report["ai_calls"] == 0
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        assert run is not None
        assert run.outcome["status"] == "human_review"
        assert run.outcome["reason"] == "publication_date_unknown"
        assert run.outcome["ai_calls"] == 0
        assert run.post_id is None


def test_fresh_guide_without_publication_date_is_left_for_the_model_and_the_owner(seeded):
    from relocant_media.ops.newsroom_triage import triage_queue

    engine, raw_id, _ = seeded
    with Session(engine) as session:
        item = session.get(RawItem, raw_id)
        item.published_at = None
        item.ingested_at = datetime.now(UTC) - timedelta(days=5)
        session.get(Source, item.source_id).source_type = "official_guide"
        session.commit()

    report = triage_queue(engine, now=datetime.now(UTC))
    assert report["decisions"] == []
    with Session(engine) as session:
        assert session.get(EditorialRun, raw_id) is None


def test_triage_withdraws_its_own_wrong_date_hold_on_a_fresh_guide(seeded):
    from relocant_media.ops.newsroom_triage import triage_queue

    engine, raw_id, _ = seeded
    with Session(engine) as session:
        item = session.get(RawItem, raw_id)
        item.published_at = None
        item.ingested_at = datetime.now(UTC) - timedelta(days=2)
        session.commit()
    assert triage_queue(engine, now=datetime.now(UTC))["metadata_deferred"] == 1  # news rule
    with Session(engine) as session:
        session.get(Source, session.get(RawItem, raw_id).source_id).source_type = "official_guide"
        session.commit()

    assert triage_queue(engine, now=datetime.now(UTC))["decisions"] == []
    with Session(engine) as session:
        assert session.get(EditorialRun, raw_id) is None  # free for the model and the owner


def test_triage_never_withdraws_a_hold_that_has_a_draft_or_receipt(seeded):
    from relocant_media.ops.newsroom_triage import triage_queue

    engine, raw_id, _ = seeded
    with Session(engine) as session:
        item = session.get(RawItem, raw_id)
        item.published_at = None
        item.ingested_at = datetime.now(UTC)
        session.commit()
    triage_queue(engine, now=datetime.now(UTC))
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        run.staging_message_id = 77
        session.get(Source, session.get(RawItem, raw_id).source_id).source_type = "official_guide"
        session.commit()
    triage_queue(engine, now=datetime.now(UTC))
    with Session(engine) as session:
        assert session.get(EditorialRun, raw_id).staging_message_id == 77


def test_guide_not_re_verified_for_thirty_days_expires_in_triage(seeded):
    from relocant_media.ops.newsroom_triage import triage_queue

    engine, raw_id, _ = seeded
    with Session(engine) as session:
        item = session.get(RawItem, raw_id)
        item.published_at = None
        item.ingested_at = datetime.now(UTC) - timedelta(days=31)
        session.get(Source, item.source_id).source_type = "official_guide"
        session.commit()

    assert triage_queue(engine, now=datetime.now(UTC))["discarded"] == 1
    with Session(engine) as session:
        assert session.get(EditorialRun, raw_id).outcome["reason"] == "guide_verification_expired"


def _enable_guide(monkeypatch):
    """Guides are switched off in the registry; the live-check mechanism is still covered."""
    source = next(item for item in server_cycle.load_source_registry(server_cycle.SOURCES)
                  if item.id == "guide_nsz_foreigner_employment")
    monkeypatch.setattr(server_cycle, "guide_sources", lambda: {source.id: source})


def _live_package(*documents):
    from types import SimpleNamespace

    return SimpleNamespace(event=SimpleNamespace(documents=[
        SimpleNamespace(source_id=source_id, url=url) for source_id, url in documents
    ]))


def test_live_check_covers_media_and_guide_sources_but_still_refuses_unknown_ones(monkeypatch):
    from relocant_media.collectors.base import RawItem

    _enable_guide(monkeypatch)

    class MediaCollector:
        async def collect_url(self, url):
            return RawItem("media_rts", url, url, "Naslov", "Telo vesti", None)

    class GuideCollector:
        async def collect(self):
            url = "https://www.nsz.gov.rs/nsz/employment-of-foreigners-in-the-republic-of-serbia/11181"
            return [RawItem("guide_nsz_foreigner_employment", "guide-x", url, "Guide", "Body", None,
                            canonical_url=url)]

    def build(source, client):
        return MediaCollector() if source.id == "media_rts" else GuideCollector()

    monkeypatch.setattr(server_cycle, "build_collector", build)
    guide_url = "https://www.nsz.gov.rs/nsz/employment-of-foreigners-in-the-republic-of-serbia/11181"
    package = _live_package(("media_rts", "https://www.rts.rs/lat/vesti/a.html"),
                            ("guide_nsz_foreigner_employment", guide_url))

    documents = server_cycle.fetch_documents(package)

    assert [(d.source_id, d.title, d.published_at) for d in documents] == [
        ("media_rts", "Naslov", None), ("guide_nsz_foreigner_employment", "Guide", None)]
    with pytest.raises(ValueError, match="unsupported live source"):
        server_cycle.fetch_documents(_live_package(("telegram_unknown", "https://t.me/x/1")))


@pytest.mark.asyncio
async def test_live_check_works_inside_a_running_event_loop(monkeypatch):
    """The owner-review cycle is async: asyncio.run inside it raised RuntimeError and blocked
    every guide and media package before the first independent review."""
    from relocant_media.collectors.base import RawItem

    _enable_guide(monkeypatch)

    class GuideCollector:
        async def collect(self):
            url = ("https://www.nsz.gov.rs/nsz/employment-of-foreigners-in-the-republic-of-"
                   "serbia/11181")
            return [RawItem("guide_nsz_foreigner_employment", "guide-x", url, "Guide", "Body",
                            None, canonical_url=url)]

    monkeypatch.setattr(server_cycle, "build_collector", lambda source, client: GuideCollector())
    package = _live_package((
        "guide_nsz_foreigner_employment",
        "https://www.nsz.gov.rs/nsz/employment-of-foreigners-in-the-republic-of-serbia/11181"))

    documents = server_cycle.fetch_documents(package)  # called synchronously from async code

    assert [d.title for d in documents] == ["Guide"]
