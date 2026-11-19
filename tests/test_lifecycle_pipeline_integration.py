from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_primary_cycle import NOW, ingest_ok
from test_primary_cycle import queue as primary_queue

from relocant_media.ops.actions_runtime import _sweep_material_lifecycle
from relocant_media.ops.primary_cycle import run_cycle
from relocant_media.storage.models import RawItem

queue = primary_queue


def test_due_retry_survives_reserve_deferral_and_uses_bounded_slots(queue, tmp_path):
    engine, process = queue
    with Session(engine) as session:
        ids = list(session.scalars(select(RawItem.id).order_by(RawItem.id)))
    due = ids[-3:]
    pending = list(due)
    calls = []

    def wrapped(raw_id):
        calls.append(raw_id)
        if raw_id in pending:
            pending.remove(raw_id)
        return process(raw_id)

    result = run_cycle(engine, city="belgrade", output=tmp_path / "retry", now=NOW,
        ingest=ingest_ok, process=wrapped, limit=5,
        deferred_items=lambda session: set(due),
        lifecycle_due=lambda: (list(pending), set()))
    assert calls[:2] == due[:2]
    assert len(calls) == len(set(calls)) == 5
    assert due[2] not in calls
    assert result["lifecycle_due_count"] == 3


def test_closed_queue_is_not_reported_as_perpetually_pending(queue, tmp_path):
    engine, _ = queue
    with Session(engine) as session:
        ids = set(session.scalars(select(RawItem.id)))
    result = run_cycle(engine, city="belgrade", output=tmp_path / "closed", now=NOW,
        ingest=ingest_ok,
        process=lambda raw_id: (_ for _ in ()).throw(AssertionError("Закрытый материал")),
        lifecycle_due=lambda: ([], ids))
    assert result["remaining_count"] == 0
    assert result["status"] == "PRIMARY_OK"


def test_batch_reserve_binding_keeps_room_for_lifecycle_retries(queue, tmp_path):
    engine, process = queue
    with Session(engine) as session:
        ids = list(session.scalars(select(RawItem.id).order_by(RawItem.id)))
    due = ids[-2:]
    seen = []

    def unavailable(items, limit):
        seen.append(limit)
        return items[:limit], {"status": "fallback", "error_type": "ModelUnavailable",
            "deferred_ids": [item["raw_item_id"] for item in items[limit:]]}

    result = run_cycle(engine, city="belgrade", output=tmp_path / "reserve", now=NOW,
        ingest=ingest_ok, process=process, preselect=unavailable, defer_failed_batch=True,
        lifecycle_due=lambda: (due, set()))
    binding = result["batch_preselection"]
    assert seen == [3]
    assert binding["gpt_slots"] == 3
    assert binding["full_review_ids"] == due
    assert len(binding["full_review_ids"]) + binding["gpt_slots"] == 5
    assert not set(due) & set(binding["gpt_candidate_ids"])


def test_lifecycle_fallback_is_bound_to_a_completed_primary_cycle(queue, tmp_path):
    from relocant_media.scheduler.models import SchedulerRun
    from relocant_media.services.gpt_fallback import _failed_ids

    engine, _ = queue
    with Session(engine) as session:
        ids = set(session.scalars(select(RawItem.id)))
    raw_id = min(ids)
    result = run_cycle(engine, city="belgrade", output=tmp_path / "fallback-binding", now=NOW,
        ingest=ingest_ok, process=lambda raw_id: None,
        lifecycle_due=lambda: ([], ids), lifecycle_fallback_items=lambda: {raw_id})
    assert result["fallback_required"] is True
    assert result["window_complete"] is False
    with Session(engine) as session:
        run = session.get(SchedulerRun, result["run_id"])
        assert run is not None and run.finished_at is not None
        assert set(_failed_ids(run, run.finished_at)) == {raw_id}


def test_intake_sweep_persists_expiry_across_engine_restart(queue):
    engine, _ = queue
    if engine.dialect.name != "sqlite":
        return
    path = engine.url.database
    assert path is not None
    from pathlib import Path

    from relocant_media.services.material_lifecycle import lifecycle_state

    _sweep_material_lifecycle(Path(path), now=NOW)
    _sweep_material_lifecycle(Path(path), now=NOW + timedelta(hours=7))
    reopened = create_engine(engine.url)
    try:
        with Session(reopened) as session:
            raw_id = session.scalar(select(RawItem.id).order_by(RawItem.id))
            state = lifecycle_state(session, raw_id, now=NOW + timedelta(hours=7))
            assert state["state"] == "blocked"
            assert state["next_action"] is None
            assert datetime.fromisoformat(state["deadline_at"]).tzinfo == UTC
    finally:
        reopened.dispose()


def test_direct_editorial_entry_cannot_reopen_closed_material_without_run(queue):
    from relocant_media.services.editorial import EditorialService
    from relocant_media.services.material_lifecycle import (
        LifecycleBlockedError,
        sweep_materials,
    )

    engine, _ = queue
    with Session(engine) as session:
        raw_id = session.scalar(select(RawItem.id).order_by(RawItem.id))
        assert raw_id is not None
        sweep_materials(session, city="belgrade", now=NOW, raw_ids={raw_id})
        sweep_materials(session, city="belgrade", now=NOW+timedelta(hours=7), raw_ids={raw_id})
        session.commit()
        with pytest.raises(LifecycleBlockedError):
            EditorialService(session, object()).prepare(raw_id)
