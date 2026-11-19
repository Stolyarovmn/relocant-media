import hashlib
import json
from datetime import timedelta

import pytest
from sqlalchemy import event
from test_gpt_fallback_service import NOW
from test_gpt_fallback_service import fallback_session as _fallback_session

from relocant_media.ai.package import canonical_json
from relocant_media.services.gpt_batch_fallback import (
    build_batch_request,
    build_ranked_request,
    pending_batch_ids,
    validate_batch_rank,
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


def test_ranked_request_also_passes_same_cycle_protected_failures(batch):
    session, raw, other, run = batch
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
    assert [candidate["raw_item_id"] for candidate in work["candidates"]] == [raw.id, other.id]


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
