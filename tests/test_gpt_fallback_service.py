import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from test_editorial_package import bind_context, package_payload, parse
from test_editorial_pipeline import _document

from relocant_media.ai.package import EditorialPackage, PackageEvidence, draft_sha256
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.editorial_products import EditorialPresentation
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import gpt_fallback
from relocant_media.services.editorial_products import present_draft
from relocant_media.services.native_map import bind_native_map_draft
from relocant_media.services.package_import import import_package
from relocant_media.sources.registry import load_source_registry
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source, Submission

NOW = datetime(2026, 11, 12, 12, tzinfo=UTC)


def seed_fallback(session: Session) -> tuple[RawItem, SchedulerRun]:
    from pathlib import Path

    config = next(
        source
        for source in load_source_registry(Path("cities/belgrade/sources"))
        if source.id == "city_beograd_news"
    )
    source = Source(
        id=config.id,
        city="belgrade",
        name=config.name,
        url=str(config.url),
        source_type=config.source_type,
        trust=Decimal(str(config.trust)),
        ownership=config.ownership,
        high_risk=config.policy.high_risk,
        auto_publish=config.policy.auto_publish,
        enabled=True,
    )
    session.add(source)
    session.flush()
    doc = _document()
    raw = RawItem(
        source_id=source.id,
        external_id="fallback-1",
        url=doc.url,
        title=doc.title,
        body=doc.body,
        published_at=NOW - timedelta(minutes=30),
        published_at_precision="instant",
        ingested_at=NOW - timedelta(minutes=30),
    )
    session.add(raw)
    session.flush()
    run = SchedulerRun(
        task_key="editorial-primary:belgrade",
        task_type="editorial_primary",
        city="belgrade",
        status="partial",
        scheduled_for=NOW - timedelta(minutes=20),
        started_at=NOW - timedelta(minutes=20),
        finished_at=NOW - timedelta(minutes=5),
        details={
            "processing_errors": [
                {
                    "raw_item_id": raw.id,
                    "error_type": "GeminiWorkerError",
                    "private_diagnostic": "НЕ ЭКСПОРТИРОВАТЬ",
                }
            ]
        },
    )
    session.add(run)
    session.commit()
    return raw, run


def package_for_request(request: dict) -> EditorialPackage:
    candidate = request["candidates"][0]
    primary = candidate["documents"][0]
    profile = load_city_profile("belgrade")
    payload = package_payload()
    payload.update(
        version=2,
        policy_version="subscription-editorial-v2",
        raw_item_id=candidate["raw_item_id"],
        source_id=candidate["source_id"],
        article_id=candidate["article_id"],
        evidence={key: primary[key] for key in PackageEvidence.model_fields},
        event={
            "event_key": candidate["event_key"],
            "editorial_rule_version": "belgrade-editorial-v2",
            "documents": candidate["documents"],
        },
    )
    payload["outcome"]["ranking"]["selection"] = {
        "policy_version": profile.policy_version,
        "profile_sha256": profile.sha256,
        "audience": "Жители района Звездара",
        "benefit": "Заранее подготовить запас воды",
        "novelty": "Опубликованы часы отключения воды",
        "why_now": "Жителям предстоит отключение",
        "rubric_id": "city_changes",
        "locality": "city",
        "value_kind": "action",
        "evidence_refs": [
            {"source_id": primary["source_id"], "url": primary["url"], "quote": primary["body"]}
        ],
    }
    presentation = EditorialPresentation.model_validate_json(
        json.dumps(candidate["presentation_by_rubric"]["city_changes"])
    )
    original = parse(payload)
    draft = present_draft(
        original.outcome.draft, presentation, category=original.outcome.ranking.category,
    )
    payload["outcome"]["draft"] = draft.model_dump(mode="json")
    payload["presentation"] = bind_native_map_draft(presentation, draft).model_dump(mode="json")
    for review in payload["reviews"]:
        review.update(
            article_id=candidate["article_id"],
            source_id=candidate["source_id"],
            reviewed_at=request["created_at"],
            draft_sha256=draft_sha256(draft),
        )
    bind_context(payload)
    return parse(payload)


@pytest.fixture
def fallback_session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        raw, run = seed_fallback(session)
        yield session, raw, run
    engine.dispose()


def test_request_and_validation_are_read_only_and_exclude_private_payload(fallback_session):
    session, raw, _ = fallback_session
    session.add(
        Submission(
            kind="news_tip",
            telegram_user_id=1,
            telegram_message_id=2,
            payload={"private": "СЕКРЕТ ПОДПИСЧИКА"},
            status="received",
        )
    )
    session.commit()
    statements = []
    engine = session.get_bind()

    def listener(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        request = gpt_fallback.build_request(session, now=NOW)
        assert request is not None
        package = package_for_request(request)
        checked = gpt_fallback.validate_fallback_package(session, request, package, now=NOW)
        assert checked == package
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert request["candidates"][0]["raw_item_id"] == raw.id
    assert request["writing_brief"]["calendar"]["timezone"] == "Europe/Belgrade"
    assert request["writing_brief"]["formats"]["events"]["words"] == [55, 110]
    assert "НЕ ЭКСПОРТИРОВАТЬ" not in json.dumps(request, ensure_ascii=False)
    assert "СЕКРЕТ ПОДПИСЧИКА" not in json.dumps(request, ensure_ascii=False)
    assert all(statement.lstrip().startswith("SELECT") for statement in statements)
    assert all("submission" not in statement.lower() for statement in statements)
    assert session.get(EditorialRun, raw.id) is None


@pytest.mark.parametrize(
    "error_type", ["GeminiCallTimeout", "GeminiWorkerError", "GeminiQuotaCooldown"]
)
def test_only_explicit_gemini_failures_enable_fallback(fallback_session, error_type):
    session, raw, run = fallback_session
    run.details = {"processing_errors": [{"raw_item_id": raw.id, "error_type": error_type}]}
    session.commit()
    assert gpt_fallback.build_request(session, now=NOW) is not None


@pytest.mark.parametrize(
    "change", ["other_error", "old_run", "running", "submissions", "future", "resolved"]
)
def test_ineligible_run_does_not_export(fallback_session, change):
    session, raw, run = fallback_session
    if change == "other_error":
        run.details = {"processing_errors": [{"raw_item_id": raw.id, "error_type": "ValueError"}]}
    elif change == "old_run":
        run.finished_at = NOW - timedelta(hours=5)
        run.started_at = NOW - timedelta(hours=6)
    elif change == "running":
        run.status = "running"
    elif change == "submissions":
        run.task_key += ":submissions"
    elif change == "future":
        run.finished_at = NOW + timedelta(minutes=1)
    else:
        session.add(EditorialRun(raw_item_id=raw.id, outcome={"status": "skipped"}, evidence={}))
    session.commit()
    assert gpt_fallback.build_request(session, now=NOW) is None


@pytest.mark.parametrize(
    "change", ["private_host", "wrong_host", "old_article", "oversized", "disabled", "policy"]
)
def test_nonpublic_stale_and_oversized_materials_are_not_exported(fallback_session, change):
    session, raw, _ = fallback_session
    if change == "private_host":
        raw.url = "http://127.0.0.1/private"
    elif change == "wrong_host":
        raw.url = "https://foreign.example/article"
    elif change == "old_article":
        raw.published_at = NOW - timedelta(days=10)
    elif change == "oversized":
        raw.body = "Большой текст. " * 5000
    elif change == "disabled":
        session.get(Source, raw.source_id).enabled = False
    else:
        session.get(Source, raw.source_id).high_risk = True
    session.commit()
    assert gpt_fallback.build_request(session, now=NOW) is None


def test_new_clean_cycle_does_not_invalidate_existing_request(fallback_session):
    session, raw, run = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    session.add(
        SchedulerRun(
            task_key=run.task_key,
            task_type=run.task_type,
            city=run.city,
            status="partial",
            scheduled_for=NOW,
            started_at=NOW,
            finished_at=NOW,
            details={"processing_errors": []},
        )
    )
    session.commit()
    assert gpt_fallback.build_request(session, now=NOW)["scheduler_run_id"] == run.id
    assert gpt_fallback.validate_fallback_package(session, request, package, now=NOW) == package


@pytest.mark.parametrize(
    "change",
    [
        "removed_error",
        "raw_changed",
        "foreign_id",
        "duplicate_id",
        "old_request",
        "profile",
        "writing_brief",
        "presentation",
    ],
)
def test_changed_request_or_current_state_blocks_package(fallback_session, change):
    session, raw, run = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    if change == "removed_error":
        run.details = {"processing_errors": []}
        session.commit()
    elif change == "raw_changed":
        raw.body += " Новые условия."
        session.commit()
    elif change == "foreign_id":
        package = package.model_copy(update={"raw_item_id": raw.id + 100})
    elif change == "duplicate_id":
        request["candidates"].append(copy.deepcopy(request["candidates"][0]))
    elif change == "old_request":
        request["created_at"] = (NOW - timedelta(hours=5)).isoformat()
    elif change == "profile":
        request["profile_sha256"] = "a" * 64
    elif change == "writing_brief":
        request["writing_brief"]["formats"]["events"]["words"] = [1, 2]
    else:
        request["candidates"][0]["presentation_by_rubric"]["city_changes"]["rubric_name"] = "Иная"
    with pytest.raises(ValueError):
        gpt_fallback.validate_fallback_package(session, request, package, now=NOW)
    assert session.get(EditorialRun, raw.id) is None


@pytest.mark.parametrize(
    "change", ["live", "stale", "future", "same_reviewer", "flags", "hold", "score", "selection"]
)
def test_missing_or_noncurrent_reviews_and_selection_fail_closed(fallback_session, change):
    session, _, _ = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    payload = package_for_request(request).model_dump(mode="json")
    review = payload["reviews"][0]
    if change == "live":
        review["live_source_checked"] = False
    elif change == "stale":
        review["reviewed_at"] = (NOW - timedelta(hours=1)).isoformat()
    elif change == "future":
        review["reviewed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif change == "same_reviewer":
        review["reviewer_id"] = payload["reviews"][1]["reviewer_id"]
    elif change == "flags":
        review["flags"] = ["не подтверждено"]
    elif change == "hold":
        review["verdict"] = "hold"
    elif change == "score":
        review["score"] = 74
    else:
        payload["outcome"]["ranking"]["selection"] = None
    bind_context(payload)
    with pytest.raises(ValueError):
        gpt_fallback.validate_fallback_package(session, request, parse(payload), now=NOW)


def test_identical_sent_replay_preserves_receipt_and_other_result_is_blocked(fallback_session):
    session, raw, _ = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    gpt_fallback.validate_fallback_package(session, request, package, now=NOW)
    stored = import_package(session, package, now=NOW)
    stored.delivery_status = "sent"
    stored.staging_message_id = 777
    post_id = stored.post_id
    session.commit()
    assert gpt_fallback.build_request(session, now=NOW) is None
    checked = gpt_fallback.validate_fallback_package(session, request, package, now=NOW)
    imported = import_package(session, checked, now=NOW)
    assert (imported.delivery_status, imported.staging_message_id, imported.post_id) == (
        "sent",
        777,
        post_id,
    )
    assert len(session.scalars(select(Post)).all()) == 1
    stored.outcome = {"status": "skipped"}
    session.commit()
    with pytest.raises(ValueError):
        gpt_fallback.validate_fallback_package(session, request, package, now=NOW)


def test_total_request_size_is_fail_closed(fallback_session, monkeypatch):
    session, _, _ = fallback_session
    monkeypatch.setattr(gpt_fallback, "MAX_REQUEST_BYTES", 100)
    assert gpt_fallback.build_request(session, now=NOW) is None


def test_only_recent_unresolved_gemini_failures_are_deferred(fallback_session):
    session, raw, run = fallback_session
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == {raw.id}
    assert not gpt_fallback.pending_gpt_ids(session, now=NOW + timedelta(hours=4))
    run.details = {"processing_errors": [{"raw_item_id": raw.id, "error_type": "ValueError"}]}
    session.commit()
    assert not gpt_fallback.pending_gpt_ids(session, now=NOW)


def test_request_lives_four_hours_from_export_not_from_worker_failure(fallback_session):
    session, _, run = fallback_session
    run.started_at = NOW - timedelta(hours=4)
    run.finished_at = NOW - timedelta(hours=3)
    session.commit()
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    assert (
        gpt_fallback.validate_fallback_package(
            session,
            request,
            package,
            now=NOW + timedelta(hours=2),
        )
        == package
    )
    with pytest.raises(ValueError, match="просрочен"):
        gpt_fallback.validate_fallback_package(
            session,
            request,
            package,
            now=NOW + timedelta(hours=4, seconds=1),
        )


def test_build_request_caps_five_unique_unresolved_candidates(fallback_session):
    session, first, run = fallback_session
    ids = [first.id]
    for number in range(2, 8):
        item = RawItem(
            source_id=first.source_id,
            external_id=f"fallback-{number}",
            url=f"https://www.beograd.rs/event-{number}.html",
            title=f"Samostalna objava {number}",
            body=f"Nova gradska vest {number}.",
            published_at=first.published_at,
            ingested_at=first.ingested_at,
            published_at_precision="instant",
        )
        session.add(item)
        session.flush()
        ids.append(item.id)
    run.details = {
        "processing_errors": [
            {"raw_item_id": raw_id, "error_type": "GeminiCallTimeout"} for raw_id in [*ids, *ids]
        ]
    }
    session.commit()
    request = gpt_fallback.build_request(session, now=NOW)
    assert [row["raw_item_id"] for row in request["candidates"]] == ids[:5]


def test_fresh_utility_notice_precedes_an_older_gemini_failure(fallback_session):
    session, first, run = fallback_session
    water = RawItem(
        source_id=first.source_id, external_id="outage",
        url="https://www.beograd.rs/lat/servisne-informacije/vodovod-i-kanalizacija/a1/x.html",
        title="Kvarovi na vodovodnoj mreži",
        body="Do 15.00\nČukarica: Ljube Rankovića 47 – Ostružnica",
        published_at=first.published_at, published_at_precision="instant",
        ingested_at=first.ingested_at,
    )
    session.add(water)
    session.flush()
    later = SchedulerRun(
        task_key=run.task_key, task_type=run.task_type, city=run.city, status="partial",
        scheduled_for=NOW - timedelta(minutes=4), started_at=NOW - timedelta(minutes=4),
        finished_at=NOW - timedelta(minutes=3),
        details={"processing_errors": [
            {"raw_item_id": water.id, "error_type": "GeminiWorkerError"},
        ]},
    )
    session.add(later)
    session.commit()
    request = gpt_fallback.build_request(session, now=NOW)
    assert request["scheduler_run_id"] == later.id
    assert [candidate["raw_item_id"] for candidate in request["candidates"]] == [water.id]


def test_unregistered_secondary_source_blocks_entire_bundle(fallback_session):
    session, first, _ = fallback_session
    source = Source(
        id="private-intake",
        city="belgrade",
        name="Закрытый источник",
        url="https://private.example",
        source_type="private",
        trust=Decimal("1"),
        ownership="unknown",
        high_risk=True,
        auto_publish=False,
        enabled=True,
    )
    session.add(source)
    session.flush()
    session.add(
        RawItem(
            source_id=source.id,
            external_id="private",
            url=source.url,
            title=first.title,
            body=first.body,
            published_at=first.published_at,
            published_at_precision="instant",
        )
    )
    session.commit()
    assert gpt_fallback.build_request(session, now=NOW) is None


def test_recent_publication_guard_is_not_bypassed_by_fallback(fallback_session):
    session, first, _ = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    other = RawItem(
        source_id=first.source_id,
        external_id="earlier-event",
        url="https://www.beograd.rs/earlier.html",
        title="Другая исходная статья",
        body="Другой текст первоисточника",
        published_at=NOW - timedelta(days=3),
    )
    post = Post(
        city="belgrade",
        category="transport",
        status="ready",
        title=package.outcome.draft.title,
        body=package.outcome.draft.body,
        confidence=Decimal("0.99"),
        published_at=NOW - timedelta(hours=1),
    )
    session.add_all([other, post])
    session.flush()
    session.add(
        EditorialRun(
            raw_item_id=other.id,
            post_id=post.id,
            outcome={"status": "ready"},
            evidence={},
            delivery_status="sent",
        )
    )
    session.commit()
    with pytest.raises(ValueError, match="повторяет"):
        gpt_fallback.validate_fallback_package(session, request, package, now=NOW)


@pytest.mark.parametrize(
    "url",
    [
        "http://user@www.beograd.rs/page",
        "http://www.beograd.rs/a\nb",
        "https://www.beograd.rs:8080/page",
        "http://www.beograd.rs\\x",
    ],
)
def test_http_urls_cannot_hide_credentials_controls_or_ports(url):
    assert gpt_fallback._public_host(url) is None


def test_registry_http_endpoint_uses_same_public_checks():
    assert gpt_fallback._public_host("http://www.beograd.rs:80/page") == "beograd.rs"
