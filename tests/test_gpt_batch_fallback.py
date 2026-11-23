import copy
import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import event, select
from test_gpt_fallback_service import NOW, _refresh_exchange, package_for_request
from test_gpt_fallback_service import fallback_session as _fallback_session

from relocant_media.ai.package import canonical_json
from relocant_media.collectors.base import RawItem as CollectedArticle
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.ops import gpt_fallback_worker as worker
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import gpt_fallback, gpt_refresh
from relocant_media.services import material_lifecycle as lifecycle
from relocant_media.services.gpt_batch_fallback import (
    build_batch_request,
    build_ranked_request,
    pending_batch_ids,
    validate_batch_rank,
)
from relocant_media.services.source_replacement import (
    SourceUnavailableError,
    record_source_replacement,
)
from relocant_media.storage.models import EditorialRun, RawItem

fallback_session = _fallback_session


def _sha(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _rank(request, selected_ids):
    return {"version": 2, "kind": "batch_preselection", "request_sha256": _sha(request),
            "selected_ids": selected_ids}


def _record_rank(session, run, request, rank):
    run.details = {**run.details, "batch_preselection": {
        **run.details["batch_preselection"],
        "status": "gpt_ranked",
        "gpt_selected_ids": rank["selected_ids"],
        "gpt_rank_sha256": _sha(rank),
        "gpt_request_sha256": rank["request_sha256"],
        "gpt_evidence_sha256": {
            str(brief["raw_item_id"]): brief["evidence_sha256"]
            for brief in request["candidates"] if brief["raw_item_id"] in rank["selected_ids"]
        },
    }}
    session.commit()


@pytest.fixture
def batch(fallback_session):
    session, raw, run = fallback_session
    other = RawItem(
        source_id=raw.source_id,
        external_id="batch-other",
        url="https://www.beograd.rs/lat/vesti/batch-other",
        title="U Zemunu izložba arhivskih fotografija",
        body="Gradski muzej prikazuje istoriju naselja." * 40,
        published_at=raw.published_at,
        published_at_precision=raw.published_at_precision,
        ingested_at=raw.ingested_at,
    )
    session.add(other)
    session.flush()
    run.details = {**run.details, "batch_preselection": {
        "status": "gpt_waiting", "gpt_slots": 1,
        "gpt_candidate_ids": [raw.id, other.id], "full_review_ids": [],
        "private_error": "ЛИЧНЫЕ ДАННЫЕ", "error_type": "GeminiWorkerError",
    }}
    session.commit()
    return session, raw, other, run


def test_ranking_resumes_same_cycle_and_releases_unselected_candidates(batch):
    session, raw, other, run = batch
    request = build_batch_request(session, now=NOW, scheduler_run_id=run.id)
    assert request["scheduler_run_id"] == run.id
    assert request["max_selected"] == 1
    assert [brief["raw_item_id"] for brief in request["candidates"]] == [raw.id, other.id]
    assert pending_batch_ids(session, now=NOW) == {raw.id, other.id}
    rank = _rank(request, [other.id])
    assert validate_batch_rank(session, request, rank, now=NOW) == (run, [other.id])
    with pytest.raises(ValueError, match="ещё не принято"):
        build_ranked_request(session, request, rank, now=NOW)
    _record_rank(session, run, request, rank)
    assert pending_batch_ids(session, now=NOW) == {other.id}
    assert build_batch_request(session, now=NOW, scheduler_run_id=run.id) is None
    work = build_ranked_request(session, request, rank, now=NOW)
    assert work["version"] == 1
    assert work["scheduler_run_id"] == run.id
    assert work["batch_request_sha256"] == _sha(request)
    assert work["batch_result_sha256"] == _sha(rank)
    assert [candidate["raw_item_id"] for candidate in work["candidates"]] == [other.id]
    assert work["candidates"][0]["documents"][0]["body"] == other.body
    assert session.get(EditorialRun, raw.id) is None
    assert session.get(EditorialRun, other.id) is None


def test_expired_batch_rank_has_distinct_reason(batch):
    session, raw, _, run = batch
    request = build_batch_request(session, now=NOW, scheduler_run_id=run.id)
    rank = _rank(request, [raw.id])

    with pytest.raises(gpt_fallback.FallbackValidationError) as stopped:
        validate_batch_rank(session, request, rank, now=NOW + timedelta(days=1))
    assert stopped.value.code == "request_expired"


def test_terminal_gpt_declines_do_not_hold_remaining_or_direct_exports(batch):
    session, raw, other, run = batch
    run.details = {**run.details, "processing_errors": [], "batch_preselection": {
        **run.details["batch_preselection"], "gpt_slots": 2}}
    session.commit()
    brief = build_batch_request(session, now=NOW)
    rank = _rank(brief, [raw.id, other.id])
    _record_rank(session, run, brief, rank)
    request = build_ranked_request(session, brief, rank, now=NOW)

    def decline(raw_id):
        payload = worker.FallbackInput.model_validate_json(canonical_json({
            "request": request,
            "result": {
                "version": 1, "request_sha256": _sha(request), "packages": [],
                "rejected": [{
                    "raw_item_id": raw_id, "category": "transport",
                    "rationale": "Исходные сведения не подтверждают значимость события.",
                }],
            },
        }))
        return worker.run_worker(session.get_bind(), payload, now=NOW)

    assert decline(raw.id)["declined"] == 1
    session.expire_all()
    before = {record.id: copy.deepcopy(record.details)
              for record in session.scalars(select(SchedulerRun))}
    remaining = build_ranked_request(session, brief, rank, now=NOW)
    assert [candidate["raw_item_id"] for candidate in remaining["candidates"]] == [other.id]
    assert {record.id: record.details for record in session.scalars(select(SchedulerRun))} == before
    assert not session.new and not session.dirty and not session.deleted
    with pytest.raises(gpt_fallback.FallbackValidationError):
        validate_batch_rank(session, brief, rank, now=NOW)
    assert decline(raw.id)["obsolete_responses"] == {"already_resolved": 1}

    assert decline(other.id)["declined"] == 1
    session.expire_all()
    direct = RawItem(source_id=raw.source_id, external_id="independent-direct",
        url="https://www.beograd.rs/lat/vesti/independent-direct",
        title="Najavljena izmena saobraćaja", body="Gradski prevoz menja trasu zbog radova." * 20,
        published_at=raw.published_at, published_at_precision=raw.published_at_precision,
        ingested_at=raw.ingested_at)
    session.add(direct)
    session.flush()
    run.details = {**run.details, "processing_errors": [
        {"raw_item_id": direct.id, "error_type": "GeminiWorkerError"}]}
    session.commit()
    before = {record.id: copy.deepcopy(record.details)
              for record in session.scalars(select(SchedulerRun))}
    assert build_ranked_request(session, brief, rank, now=NOW) is None
    direct_request = gpt_fallback.build_request(session, now=NOW)
    assert [candidate["raw_item_id"] for candidate in direct_request["candidates"]] == [direct.id]
    assert {record.id: record.details for record in session.scalars(select(SchedulerRun))} == before
    assert not session.new and not session.dirty and not session.deleted


def test_approved_batch_slot_moves_to_live_source_and_reopens_on_preview(batch, monkeypatch):
    session, raw, other, run = batch
    origin_id, other_id = raw.id, other.id
    run.details = {**run.details, "processing_errors": []}
    session.commit()
    brief = build_batch_request(session, now=NOW)
    rank = _rank(brief, [origin_id])
    _record_rank(session, run, brief, rank)
    original = build_ranked_request(session, brief, rank, now=NOW)
    url = "https://www.cukarica.rs/vesti/a123458/"
    replacement = CollectedArticle(
        "cukarica_news", "a123458", url, raw.title, raw.body, raw.published_at,
        published_at_precision="instant",
    )

    async def collect(_links):
        return [SourceUnavailableError("city_beograd_news"), replacement]

    monkeypatch.setattr(gpt_refresh, "_collect", collect)
    engine = session.get_bind()
    session.rollback()
    new = gpt_refresh.refresh_gpt_evidence(
        engine, _refresh_exchange(original, origin_id, [url]), now=NOW,
    )
    candidate, = new["candidates"]
    target = candidate["raw_item_id"]
    assert target not in {origin_id, other_id}
    assert pending_batch_ids(session, now=NOW) == {target}
    assert build_ranked_request(session, brief, rank, now=NOW) == new
    gpt_fallback.validate_fallback_candidate(session, new, target, now=NOW)
    with pytest.raises(ValueError, match="Исходный сбой"):
        gpt_fallback.validate_fallback_candidate(session, original, origin_id, now=NOW)


def test_batch_export_reads_only_public_short_fragments(batch):
    session, raw, other, run = batch
    other.url = "https://unregistered.example.org/private"
    other.body = "ЛИЧНЫЙ ТЕКСТ"
    raw.body = ("Осмотрены документы района. " * 40) + "КОНЕЦ ТЕКСТА"
    session.commit()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        request = build_batch_request(session, now=NOW, scheduler_run_id=run.id)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert all(statement.lstrip().startswith("SELECT") for statement in statements)
    candidate = request["candidates"][0]
    assert candidate["raw_item_id"] == raw.id
    assert len(candidate["body_excerpt"]) == 900
    assert len(request["candidates"]) == 1
    serialized = json.dumps(request, ensure_ascii=False)
    assert not any(text in serialized for text in ("ЛИЧНЫЕ ДАННЫЕ", "ЛИЧНЫЙ ТЕКСТ",
                                                    "КОНЕЦ ТЕКСТА", "unregistered.example.org"))


def test_batch_rank_cannot_include_extra_ids_or_override_budget(batch):
    session, raw, other, run = batch
    request = build_batch_request(session, now=NOW, scheduler_run_id=run.id)
    for selected in [[raw.id, other.id], [raw.id, raw.id], [True], [other.id + 1]]:
        with pytest.raises(ValueError):
            validate_batch_rank(session, request, _rank(request, selected), now=NOW)
    forged = {**request, "max_selected": 2}
    with pytest.raises(ValueError):
        validate_batch_rank(session, forged, _rank(forged, [raw.id, other.id]), now=NOW)


def test_batch_rank_is_not_reused_for_changed_evidence_or_wrong_cycle(batch):
    session, _, other, run = batch
    request = build_batch_request(session, now=NOW, scheduler_run_id=run.id)
    rank = _rank(request, [other.id])
    wrong_cycle = {**request, "scheduler_run_id": run.id + 99}
    with pytest.raises(ValueError):
        validate_batch_rank(session, wrong_cycle, _rank(wrong_cycle, [other.id]), now=NOW)
    other.body += "Изменение за пределами 900 символов."
    session.commit()
    with pytest.raises(ValueError, match="изменились"):
        validate_batch_rank(session, request, rank, now=NOW)


def test_committed_batch_rank_is_idempotent_and_never_redelivers(batch):
    session, raw, other, run = batch
    request = build_batch_request(session, now=NOW)
    rank = _rank(request, [other.id])
    _record_rank(session, run, request, rank)
    session.add(EditorialRun(raw_item_id=other.id, outcome={"status": "ready"}, evidence={},
                             delivery_status="sent"))
    session.commit()
    assert validate_batch_rank(session, request, rank, now=NOW) == (run, [other.id])
    assert pending_batch_ids(session, now=NOW) == set()
    assert build_ranked_request(session, request, rank, now=NOW) is None
    with pytest.raises(ValueError, match="другой порядок"):
        validate_batch_rank(session, request, _rank(request, [raw.id]), now=NOW)


def test_ranked_source_change_releases_only_the_affected_candidate(batch):
    session, _, other, run = batch
    request = build_batch_request(session, now=NOW)
    rank = _rank(request, [other.id])
    _record_rank(session, run, request, rank)
    other.body += "После рейтинга изменился источник."
    session.commit()
    assert pending_batch_ids(session, now=NOW) == set()
    assert build_ranked_request(session, request, rank, now=NOW) is None
    run.details = {**run.details, "batch_preselection": {
        **run.details["batch_preselection"], "gpt_evidence_sha256": {str(other.id): "0" * 64},
    }}
    session.commit()
    with pytest.raises(ValueError, match="другой порядок"):
        validate_batch_rank(session, request, rank, now=NOW)


@pytest.mark.parametrize("after_primary_replacement", [False, True])
def test_ranked_request_also_passes_same_cycle_protected_failures(batch, after_primary_replacement):
    session, raw, other, run = batch
    expected_id = raw.id
    if after_primary_replacement:
        url = "https://www.cukarica.rs/vesti/a123459/"
        replacement = CollectedArticle(
            "cukarica_news", "a123459", url, raw.title, raw.body, raw.published_at,
            published_at_precision="instant",
        )
        config = gpt_fallback._registry()["cukarica_news"]
        expected_id = record_source_replacement(
            session, original=raw, replacement=replacement, config=config, now=NOW,
            profile=load_city_profile("belgrade"),
        )
        run.details = {**run.details, "processing_errors": [
            {"raw_item_id": expected_id, "error_type": "GeminiWorkerError"},
        ]}
    run.details = {**run.details, "batch_preselection": {
        **run.details["batch_preselection"],
        "gpt_candidate_ids": [other.id],
        "full_review_ids": [raw.id],
    }}
    session.commit()
    request = build_batch_request(session, now=NOW)
    rank = _rank(request, [other.id])
    _record_rank(session, run, request, rank)
    work = build_ranked_request(session, request, rank, now=NOW)
    assert [candidate["raw_item_id"] for candidate in work["candidates"]] == [
        expected_id, other.id,
    ]


def test_empty_accepted_choice_and_expired_runs_release_the_queue(batch):
    session, _, _, run = batch
    request = build_batch_request(session, now=NOW)
    rank = _rank(request, [])
    assert validate_batch_rank(session, request, rank, now=NOW) == (run, [])
    _record_rank(session, run, request, rank)
    assert build_ranked_request(session, request, rank, now=NOW) is None
    assert pending_batch_ids(session, now=NOW) == set()
    with pytest.raises(ValueError, match="сбой"):
        validate_batch_rank(session, request, rank, now=NOW + timedelta(hours=4))
    assert build_batch_request(session, now=NOW + timedelta(hours=4)) is None


@pytest.mark.parametrize("error_type", ["ValueError", "ValidationError", "JSONDecodeError"])
def test_legacy_response_handoff_releases_candidates_and_rejects_old_results(batch, error_type):
    session, raw, _, run = batch
    brief = build_batch_request(session, now=NOW)
    rank = _rank(brief, [raw.id])
    _record_rank(session, run, brief, rank)
    work = build_ranked_request(session, brief, rank, now=NOW)
    package = package_for_request(work)
    run.details = {**run.details, "processing_errors": [], "batch_preselection": {
        **run.details["batch_preselection"], "error_type": error_type}}
    session.commit()
    assert pending_batch_ids(session, now=NOW) == set()
    assert build_batch_request(session, now=NOW) is None
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == set()
    with pytest.raises(gpt_fallback.FallbackValidationError):
        validate_batch_rank(session, brief, rank, now=NOW)
    with pytest.raises(gpt_fallback.FallbackValidationError):
        gpt_fallback.validate_fallback_package(session, work, package, now=NOW)
    assert session.get(EditorialRun, raw.id) is None


def test_explicit_provider_error_uses_location_not_exception_name(batch):
    session, raw, other, run = batch
    run.details = {**run.details, "batch_preselection": {
        **run.details["batch_preselection"], "error_type": "ValueError", "error_stage": "provider"}}
    session.commit()
    assert pending_batch_ids(session, now=NOW) == {raw.id, other.id}
    assert build_batch_request(session, now=NOW) is not None


def test_batch_handoff_expires_from_finished_cycle_without_block_or_new_attempt(batch, monkeypatch):
    session, raw, _, run = batch
    initial = lifecycle.lifecycle_state(session, raw.id, now=NOW)
    session.commit()
    finished = run.finished_at
    expiry = finished+lifecycle.FALLBACK_WAIT
    before = expiry-timedelta(seconds=1)
    brief = build_batch_request(session, now=NOW)
    rank = _rank(brief, [raw.id])
    _record_rank(session, run, brief, rank)
    work = build_ranked_request(session, brief, rank, now=before)
    assert work is not None and work["created_at"] == before.isoformat()
    package = package_for_request(work)
    assert pending_batch_ids(session, now=before) == {raw.id}
    assert pending_batch_ids(session, now=expiry) == set()
    assert build_batch_request(session, now=expiry) is None
    with pytest.raises(gpt_fallback.FallbackValidationError):
        validate_batch_rank(session, brief, rank, now=expiry)
    with pytest.raises(gpt_fallback.FallbackValidationError):
        gpt_fallback.validate_fallback_package(session, work, package, now=expiry)
    monkeypatch.setattr(gpt_refresh, "_collect",
                        lambda *a: pytest.fail("Поздний запрос не читает сеть"))
    with pytest.raises(gpt_fallback.FallbackValidationError):
        gpt_refresh.refresh_gpt_evidence(session.get_bind(), _refresh_exchange(work, raw.id, []),
                                         now=expiry)
    assert run.finished_at == finished and session.get(EditorialRun, raw.id) is None
    assert not lifecycle.is_terminal_blocked(session, raw.id, now=expiry)
    assert not session.new and not session.dirty
    assert lifecycle.before_attempt(session, raw.id, now=expiry)
    state = lifecycle.lifecycle_state(session, raw.id, now=expiry)
    assert state["attempts"] == 1 and state["fallback_attempts"] == 0
    assert state["deadline_at"] == initial["deadline_at"]


def test_material_deadline_and_terminal_owner_close_remain_stricter_than_batch_lease(batch):
    session, raw, other, _ = batch
    lifecycle.lifecycle_state(session, raw.id, now=NOW)
    lifecycle.close_by_owner(session, other.id, now=NOW)
    from relocant_media.scheduler.models import SchedulerRun

    record = session.scalar(select(SchedulerRun).where(
        SchedulerRun.task_key == f"material-lifecycle:belgrade:{raw.id}"))
    record.details = {**record.details, "deadline_at": (NOW+timedelta(seconds=30)).isoformat()}
    session.commit()
    assert pending_batch_ids(session, now=NOW) == {raw.id}
    assert pending_batch_ids(session, now=NOW+timedelta(seconds=30)) == set()
    assert session.get(EditorialRun, raw.id) is None
