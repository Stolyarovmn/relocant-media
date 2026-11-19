"""Вспомогательный порядок не останавливает полный разбор и не подменяет допуск."""

import json

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_primary_cycle import NOW, ingest_ok
from test_primary_cycle import editorial_session as _editorial_session
from test_primary_cycle import queue as _queue

from relocant_media.ai.batch_preselection import preselect_candidates
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.ops.primary_cycle import run_cycle
from relocant_media.ops.server_cycle import lifecycle_due_order
from relocant_media.services import material_lifecycle as core
from relocant_media.services.material_lifecycle_runtime import MaterialLifecycleRuntime
from relocant_media.storage.models import RawItem, Source

queue = _queue
editorial_session = _editorial_session


class PartialResponse:
    def generate_json(self, **kwargs):
        rows = json.loads(kwargs["prompt"])["candidates"]
        assert kwargs["response_schema"]["properties"]["ranking"]["minItems"] == len(rows)
        return json.dumps({"ranking": [{"raw_item_id": row["raw_item_id"],
                                        "reason": "Неполный тестовый порядок"}
                                       for row in rows[:1]]})


def precise(engine):
    with Session(engine) as session:
        rows = list(session.scalars(select(RawItem).order_by(RawItem.id)))
        for row in rows:
            row.published_at_precision = "instant"
        session.commit()
        return [row.id for row in rows]


def choose(items, limit):
    return preselect_candidates(items, PartialResponse(), profile=load_city_profile("belgrade"),
                                limit=limit, now=NOW)


def test_response_error_keeps_five_candidates_on_full_primary_path(queue, tmp_path):
    engine, process = queue
    precise(engine)
    calls = []

    def full(raw_id):
        calls.append(raw_id)
        return process(raw_id)

    result = run_cycle(engine, city="belgrade", output=tmp_path / "response-error", now=NOW,
        ingest=ingest_ok, process=full, preselect=choose, defer_failed_batch=True)
    assert len(calls) == 5
    assert result["batch_preselection"]["error_stage"] == "response"
    assert result["batch_preselection"]["status"] == "fallback"
    assert "gpt_candidate_ids" not in result["batch_preselection"]
    assert not result.get("fallback_required", False)
    assert result["gpt_deferred_count"] == 0 and result["processing_errors"] == []


def test_terminal_and_future_lifecycle_exclusions_are_not_gpt_pending(queue, tmp_path):
    engine, process = queue
    ids = precise(engine)
    excluded = set(ids[:3]) | set(range(10000, 20000))
    result = run_cycle(engine, city="belgrade", output=tmp_path / "counts", now=NOW,
        ingest=ingest_ok, process=process, lifecycle_due=lambda: ([], excluded),
        deferred_items=lambda session: {ids[3]})
    assert result["gpt_deferred_count"] == 1
    assert result["lifecycle_excluded_count"] == len(excluded)
    result = run_cycle(engine, city="belgrade", output=tmp_path / "no-gpt", now=NOW,
        ingest=ingest_ok, process=process, lifecycle_due=lambda: ([], excluded),
        deferred_items=lambda session: set())
    assert result["gpt_deferred_count"] == 0 and not result.get("fallback_required", False)


def test_retry_and_audit_each_get_a_normal_slot_with_stable_oldest_order(queue, tmp_path):
    engine, process = queue
    ids = precise(engine)
    audit_ids, retry_ids = ids[-4:-2], ids[-2:]
    due = lifecycle_due_order(reversed(audit_ids), reversed(retry_ids))
    assert due == [retry_ids[0], audit_ids[0], retry_ids[1], audit_ids[1]]
    calls = []

    def full(raw_id):
        calls.append(raw_id)
        return process(raw_id)

    run_cycle(engine, city="belgrade", output=tmp_path / "fair", now=NOW,
        ingest=ingest_ok, process=full, lifecycle_due=lambda: (due, set(due)))
    assert calls[:2] == [retry_ids[0], audit_ids[0]]
    assert retry_ids[1] not in calls and audit_ids[1] not in calls and len(calls) == 5


def test_response_fallback_does_not_revive_terminal_or_high_risk_material(queue, tmp_path):
    engine, process = queue
    ids = precise(engine)
    with Session(engine) as session:
        core.close_by_owner(session, ids[0], now=NOW)
        session.add(Source(id="synthetic_risky", city="belgrade", name="Рискованный источник",
            url="https://risky.example.test", source_type="news_media", trust=1,
            ownership="media", auto_publish=True, high_risk=True, enabled=True))
        session.flush()
        risky = RawItem(source_id="synthetic_risky", external_id="risky",
            url="https://risky.example.test/news", title="Синтетическая политическая тема",
            body="Синтетические сведения для проверки ограничений.", published_at=NOW,
            published_at_precision="instant", ingested_at=NOW)
        session.add(risky)
        session.commit()
        risky_id = risky.id
    lifecycle = MaterialLifecycleRuntime(engine, "belgrade", PartialResponse(), now=NOW)

    def due():
        sweep = lifecycle.reconcile()
        return lifecycle_due_order(sweep.rejection_audit_ids, sweep.due_ids), set(
            sweep.excluded_ids)

    calls = []

    def full(raw_id):
        calls.append(raw_id)
        return process(raw_id)

    run_cycle(engine, city="belgrade", output=tmp_path / "guards", now=NOW,
        ingest=ingest_ok, process=full, preselect=choose, defer_failed_batch=True,
        lifecycle_due=due)
    assert len(calls) == 5 and not {ids[0], risky_id} & set(calls)
    with Session(engine) as session:
        assert core.is_terminal_blocked(session, ids[0], now=NOW)
        assert core.is_terminal_blocked(session, risky_id, now=NOW)
