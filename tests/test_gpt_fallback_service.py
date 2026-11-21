import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from facts_check_helpers import bind_facts_check
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session
from test_editorial_package import bind_context, package_payload, parse
from test_editorial_pipeline import _document

from relocant_media.ai.package import (
    EditorialPackage,
    PackageEvidence,
    canonical_json,
    draft_sha256,
)
from relocant_media.collectors.base import RawItem as CollectedRawItem
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.editorial_products import EditorialPresentation
from relocant_media.domain.policy import SOURCE_REPLACED_REASON
from relocant_media.ops import server_cycle
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import gpt_fallback, gpt_refresh
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
    for claim in payload["outcome"]["claims"]["claims"]:
        for citation in claim["citations"]:
            citation.update(source_id=primary["source_id"], url=primary["url"])
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
    bind_facts_check(payload)
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


def _refresh_exchange(request: dict, raw_id: int, urls: list[str]):
    return gpt_refresh.RefreshInput.model_validate({
        "request": request,
        "result": {
            "version": 3, "kind": "evidence_refresh",
            "request_sha256": hashlib.sha256(canonical_json(request).encode()).hexdigest(),
            "raw_item_id": raw_id, "urls": urls,
        },
    })


def test_refresh_recollects_live_evidence_and_leaves_the_story_unresolved(
    fallback_session, monkeypatch,
):
    session, raw, run = fallback_session
    raw_id, cycle_id = raw.id, run.id
    request = gpt_fallback.build_request(session, now=NOW)
    assert request is not None
    alternative = "https://www.cukarica.rs/vesti/example"
    exchange = _refresh_exchange(request, raw_id, [alternative])
    engine = session.get_bind()
    new_body = f"{raw.body} Radovi su planirani zbog popravke mreže."
    articles = [
        CollectedRawItem(
            raw.source_id, raw.external_id, raw.url, raw.title, new_body, raw.published_at,
            published_at_precision="instant",
        ),
        CollectedRawItem(
            "cukarica_news", "alternative-url", alternative, raw.title, new_body,
            raw.published_at, published_at_precision="instant",
        ),
    ]
    session.rollback()

    async def collect(links):
        assert [(config.id, url) for config, url in links] == [
            (item.source_id, item.url) for item in articles
        ]
        return articles

    monkeypatch.setattr(gpt_refresh, "_collect", collect)
    refreshed_at = NOW + timedelta(minutes=5)
    refreshed = gpt_refresh.refresh_gpt_evidence(engine, exchange, now=refreshed_at)
    assert refreshed["scheduler_run_id"] == cycle_id
    assert refreshed["created_at"] == refreshed_at.isoformat()
    candidate, = refreshed["candidates"]
    assert candidate["raw_item_id"] == raw_id
    assert [doc["url"] for doc in candidate["documents"]] == [articles[0].url, alternative]
    assert all(doc["body"] == new_body for doc in candidate["documents"])
    assert session.get(EditorialRun, raw_id) is None
    assert session.get(SchedulerRun, cycle_id).details["gpt_evidence_refresh"][str(raw_id)][
        "attempts"
    ] == 1
    gpt_fallback.validate_fallback_candidate(session, refreshed, raw_id, now=refreshed_at)
    with pytest.raises(ValueError, match="Снимок события"):
        gpt_fallback.validate_fallback_candidate(session, request, raw_id, now=refreshed_at)


def test_refresh_substitutes_404_with_same_event_and_imports_only_live_evidence(
    fallback_session, monkeypatch,
):
    session, raw, cycle = fallback_session
    raw_id, cycle_id = raw.id, cycle.id
    original_url, original_body = raw.url, raw.body
    original_request = gpt_fallback.build_request(session, now=NOW)
    assert original_request is not None
    url = "https://www.cukarica.rs/vesti/a123456/"
    replacement = CollectedRawItem(
        "cukarica_news", "a123456", url, raw.title, raw.body, raw.published_at,
        published_at_precision="instant",
    )
    fetches = []

    async def fetch(config, exact_url, _client):
        fetches.append((config.id, exact_url))
        if exact_url == original_url:
            request = httpx.Request("GET", original_url)
            httpx.Response(404, request=request).raise_for_status()
        assert config.id == replacement.source_id and exact_url == replacement.url
        return replacement

    monkeypatch.setattr(server_cycle, "fetch_public_article", fetch)
    engine = session.get_bind()
    session.rollback()
    result = gpt_refresh.refresh_gpt_evidence(
        engine, _refresh_exchange(original_request, raw_id, [url]), now=NOW,
    )
    assert set(fetches) == {("city_beograd_news", original_url), ("cukarica_news", url)}
    candidate, = result["candidates"]
    target = candidate["raw_item_id"]
    assert target != raw_id
    assert [document["url"] for document in candidate["documents"]] == [url]
    assert candidate["presentation_by_rubric"]["city_changes"]["illustration"]["source_url"] == url
    assert session.get(RawItem, raw_id, populate_existing=True).body == original_body
    marker = session.get(EditorialRun, raw_id, populate_existing=True)
    assert marker.post_id is None
    assert marker.outcome["reason"] == SOURCE_REPLACED_REASON
    assert marker.outcome["replacement_raw_item_id"] == target
    receipt = session.get(SchedulerRun, cycle_id, populate_existing=True).details[
        "gpt_evidence_refresh"][str(raw_id)]
    assert receipt["replacement_raw_item_id"] == target
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == {target}
    assert gpt_fallback.build_request(session, now=NOW) == result
    with pytest.raises(ValueError, match="Исходный сбой"):
        gpt_fallback.validate_fallback_candidate(session, original_request, raw_id, now=NOW)
    package = package_for_request(result)
    gpt_fallback.validate_fallback_package(session, result, package, now=NOW)
    assert import_package(session, package, now=NOW).outcome["status"] == "ready"
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == set()


@pytest.mark.parametrize(("failure", "wrong_event"), [
    (ValueError("Изменилась разметка страницы"), False),
    (httpx.ReadTimeout("Не получен ответ"), True),
])
def test_refresh_leaves_original_when_absence_or_event_is_unproven(
    fallback_session, monkeypatch, failure, wrong_event,
):
    session, raw, run = fallback_session
    raw_id, cycle_id, original_url = raw.id, run.id, raw.url
    request = gpt_fallback.build_request(session, now=NOW)
    assert request is not None
    url = "https://www.cukarica.rs/vesti/a123457/"
    replacement = CollectedRawItem(
        "cukarica_news", "a123457", url,
        "U Zemunu izložba" if wrong_event else raw.title,
        "Muzej najavljuje izložbu dela slikara 20. novembra." if wrong_event else raw.body,
        raw.published_at, published_at_precision="instant",
    )

    async def fetch(_config, exact_url, _client):
        if exact_url == original_url:
            raise failure
        return replacement

    monkeypatch.setattr(server_cycle, "fetch_public_article", fetch)
    engine = session.get_bind()
    session.rollback()
    with pytest.raises(ValueError if wrong_event else ExceptionGroup):
        gpt_refresh.refresh_gpt_evidence(
            engine, _refresh_exchange(request, raw_id, [url]), now=NOW,
        )
    assert session.get(EditorialRun, raw_id) is None
    assert "gpt_evidence_refresh" not in session.get(SchedulerRun, cycle_id).details
    assert session.scalar(select(RawItem).where(RawItem.url == url)) is None


@pytest.mark.parametrize(("url", "error"), [
    ("https://unregistered.example/announcement", "не зарегистрирован"),
    ("https://www.cukarica.rs/vesti/example", "просрочен"),
])
def test_refresh_refuses_unregistered_source_or_expired_request_before_network(
    fallback_session, monkeypatch, url, error,
):
    session, raw, _ = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    assert request is not None
    exchange = _refresh_exchange(request, raw.id, [url])

    async def collect(_links):
        pytest.fail("Недопустимый запрос не должен обращаться к сборщику")

    monkeypatch.setattr(gpt_refresh, "_collect", collect)
    now = NOW + timedelta(hours=5) if error == "просрочен" else NOW
    with pytest.raises(ValueError, match=error):
        gpt_refresh.refresh_gpt_evidence(session.get_bind(), exchange, now=now)


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
    assert request["writing_brief"]["version"] == "ru-local-news-v3"
    assert "ближайший подтверждённый шаг" in request["writing_brief"]["common"]
    assert request["writing_brief"]["calendar"]["timezone"] == "Europe/Belgrade"
    assert request["writing_brief"]["formats"]["events"]["words"] == [55, 110]
    from relocant_media.ai.draft_coverage import FACTS_COVERAGE_INSTRUCTION, DraftFactsCheck

    contract = request["writing_brief"]["facts_review_contract"]
    assert contract["instruction"] == FACTS_COVERAGE_INSTRUCTION
    assert contract["schema"] == DraftFactsCheck.model_json_schema()
    assert contract["placement"] == "package.reviews[role=facts].facts_check"
    assert request["version"] == 1
    assert "НЕ ЭКСПОРТИРОВАТЬ" not in json.dumps(request, ensure_ascii=False)
    assert "СЕКРЕТ ПОДПИСЧИКА" not in json.dumps(request, ensure_ascii=False)
    assert all(statement.lstrip().startswith("SELECT") for statement in statements)
    assert all("submission" not in statement.lower() for statement in statements)
    assert session.get(EditorialRun, raw.id) is None


def test_fallback_builds_the_map_for_each_rubric(fallback_session, monkeypatch):
    session, _, _ = fallback_session
    original = gpt_fallback.build_presentation
    rubrics = []

    def build(*args, **kwargs):
        rubrics.append(kwargs["rubric_id"])
        result = original(*args, **kwargs)
        return result.model_copy(update={"event_place": {"verified": True}}) if (
            kwargs["rubric_id"] == "weekend") else result

    monkeypatch.setattr(gpt_fallback, "build_presentation", build)
    request = gpt_fallback.build_request(session, now=NOW)
    plans = request["candidates"][0]["presentation_by_rubric"]
    assert rubrics == [row.id for row in load_city_profile("belgrade").rubrics]
    assert "event_place" not in plans["city_changes"]
    assert plans["weekend"]["event_place"] == {"verified": True}


def test_perfect_reviews_cannot_publish_a_new_physical_event_without_its_map(fallback_session):
    from relocant_media.ai.package import evaluate_package

    session, _, _ = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    payload = package_for_request(request).model_dump(mode="json")
    payload["outcome"]["ranking"]["category"] = "events"
    payload["outcome"]["ranking"]["selection"]["rubric_id"] = "weekend"
    presentation = EditorialPresentation.model_validate_json(json.dumps(
        request["candidates"][0]["presentation_by_rubric"]["weekend"]))
    draft = parse(payload).outcome.draft.model_copy(update={
        "title": "Концерт в Белграде", "body": "Площадка: Zappa Baza."})
    draft = present_draft(draft, presentation, category="events")
    payload["outcome"]["draft"] = draft.model_dump(mode="json")
    payload["presentation"] = presentation.model_dump(mode="json")
    for review in payload["reviews"]:
        review.update(draft_sha256=draft_sha256(draft), score=100)
    bind_facts_check(payload)
    bind_context(payload)
    outcome = evaluate_package(parse(payload), now=NOW)
    assert outcome.status == "human_review"
    assert outcome.reason == "event_place_requires_verification"


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


def test_newer_failure_does_not_displace_older_failure_of_same_priority(fallback_session):
    session, first, run = fallback_session
    second = RawItem(
        source_id=first.source_id, external_id="fallback-newer",
        url="https://www.beograd.rs/lat/beoinfo-vesti/a116900/Izlozba.html",
        title="Izložba u Beogradu",
        body="Izložba će biti otvorena 18. novembra u 19 časova u Kulturnom centru.",
        published_at=first.published_at, published_at_precision="instant",
        ingested_at=first.ingested_at,
    )
    session.add(second)
    session.flush()
    later = SchedulerRun(
        task_key=run.task_key, task_type=run.task_type, city=run.city, status="partial",
        scheduled_for=NOW - timedelta(minutes=4), started_at=NOW - timedelta(minutes=4),
        finished_at=NOW - timedelta(minutes=3),
        details={"processing_errors": [
            {"raw_item_id": second.id, "error_type": "GeminiWorkerError"},
        ]},
    )
    session.add(later)
    session.commit()

    request = gpt_fallback.build_request(session, now=NOW)
    assert request["scheduler_run_id"] == run.id
    assert [candidate["raw_item_id"] for candidate in request["candidates"]] == [first.id]
    explicit = gpt_fallback.build_request(session, now=NOW, scheduler_run_id=later.id)
    assert explicit["scheduler_run_id"] == later.id


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


def test_official_city_event_is_not_blocked_by_an_older_media_failure(fallback_session):
    from pathlib import Path

    session, official, run = fallback_session
    official.title = "Izložba u Beogradu"
    official.body = "Izložba će biti otvorena 18. novembra u 19 časova u Kulturnom centru."
    official.url = "https://www.beograd.rs/lat/beoinfo-vesti/a116900/Izlozba.html"
    config = next(source for source in load_source_registry(Path("cities/belgrade/sources"))
                  if source.id == "media_n1_srbija")
    session.add(Source(
        id=config.id, city="belgrade", name=config.name, url=str(config.url),
        source_type=config.source_type, trust=Decimal(str(config.trust)),
        ownership=config.ownership, high_risk=config.policy.high_risk,
        auto_publish=config.policy.auto_publish, enabled=True,
    ))
    session.flush()
    media = RawItem(
        source_id=config.id, external_id="national-politics",
        url="https://n1info.rs/vesti/politicka-izjava/", title="Politička izjava",
        body="Izjava o radu nacionalne skupštine.", published_at=official.published_at,
        published_at_precision="instant", ingested_at=official.ingested_at,
    )
    session.add(media)
    session.flush()
    older = SchedulerRun(
        task_key=run.task_key, task_type=run.task_type, city=run.city, status="partial",
        scheduled_for=NOW - timedelta(minutes=50), started_at=NOW - timedelta(minutes=50),
        finished_at=NOW - timedelta(minutes=40),
        details={"processing_errors": [
            {"raw_item_id": media.id, "error_type": "GeminiWorkerError"},
        ]},
    )
    session.add(older)
    session.commit()
    request = gpt_fallback.build_request(session, now=NOW)
    assert request["scheduler_run_id"] == run.id
    assert [candidate["raw_item_id"] for candidate in request["candidates"]] == [official.id]
    assert session.get(EditorialRun, official.id) is None
    assert session.get(EditorialRun, media.id) is None


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
