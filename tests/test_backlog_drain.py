import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ops import backlog_drain as drain
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import material_lifecycle as core
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source
from relocant_media.storage.sqlite_schema import initialize_sqlite


class Provider:
    def __init__(self):
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs["task"])
        assert kwargs["task"] == "batch_preselect"
        payload = json.loads(kwargs["prompt"])
        assert len(payload["candidates"]) <= 30
        return json.dumps({"ranking": [{"raw_item_id": row["raw_item_id"],
                                       "reason": "Проверить исходные факты."}
                                      for row in payload["candidates"]]})


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / "state.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    now = datetime.now(UTC)
    with Session(engine) as session:
        session.add(Source(id="official", city="belgrade", name="Официальный источник",
            url="https://example.test", source_type="official_news", trust=1,
            ownership="official", auto_publish=True, enabled=True, high_risk=False))
        session.commit()
    # Selector имеет собственные интеграционные тесты. Здесь fake не вызывает сеть,
    # а реальные run_cycle/runtime/core сохраняют аренды, сроки и бюджеты попыток.
    def candidates(session, *, allowed_raw_ids=None, excluded_raw_ids=None, limit=5, **kwargs):
        assert allowed_raw_ids is not None
        assert limit in {1, 2, 3, 4, 5, 30}
        reviewed = set(session.scalars(select(EditorialRun.raw_item_id)))
        rows = list(session.scalars(select(RawItem).where(
            RawItem.id.in_(allowed_raw_ids-reviewed-(excluded_raw_ids or set())),
        ).order_by(RawItem.id)))
        return {"candidates": [{"raw_item_id": row.id, "source_id": row.source_id,
                    "source": {"type": "official_news", "ownership": "official"},
                    "evidence": {"title": row.title, "body": row.body, "url": row.url,
                        "published_at": row.published_at.isoformat(),
                        "published_at_precision": row.published_at_precision}}
                   for row in rows[:limit]], "eligible_count": len(rows),
                "candidate_count": min(limit, len(rows)), "missing_published_at_count": 0}
    monkeypatch.setattr("relocant_media.ops.primary_cycle.select_candidates", candidates)
    monkeypatch.setattr(drain, "media_sources", lambda: {})
    monkeypatch.delenv("EDITORIAL_GPT_FALLBACK_ENABLED", raising=False)
    yield engine, now, tmp_path
    engine.dispose()


def seed(engine, now, count, *, first=1, ingested=None):
    with Session(engine) as session:
        session.add_all(RawItem(id=index, source_id="official", external_id=str(index),
            url=f"https://example.test/{index}", title=f"Beograd: новость {index}",
            body=f"PRIVATE_BODY: отдельные исходные факты {index} в Белграде.",
            published_at=now-timedelta(hours=1), published_at_precision="instant",
            ingested_at=ingested or now) for index in range(first, first+count))
        session.commit()


def block(engine, calls):
    def process(raw_id):
        calls.append(raw_id)
        with Session(engine) as session:
            core.block_unsupported_repair(session, raw_id, now=datetime.now(UTC))
            session.commit()
        return {"raw_item_id": raw_id, "status": "blocked"}
    return process


def campaign(engine):
    with Session(engine) as session:
        return session.scalar(select(SchedulerRun).where(
            SchedulerRun.task_type == drain.TASK_TYPE)).details


def test_strict_claim_limit_frozen_cohort_and_hop_replay(state):
    engine, now, output = state
    seed(engine, now, 8)
    seed(engine, now, 1, first=9, ingested=now+timedelta(seconds=1))
    calls, provider = [], Provider()
    first = drain.process_existing(engine, now, output / "one", provider,
        max_attempts=3, process=block(engine, calls), runs_left=3)
    assert first["cohort_total"] == 8 and first["attempts_used"] == 3
    assert first["remaining"] == 5 and len(calls) == 3
    replay = drain.process_existing(engine, now, output / "replay", provider,
        max_attempts=3, process=block(engine, calls), runs_left=3)
    assert replay["replay"] and not replay["needs_continue"] and len(calls) == 3
    seed(engine, now, 1, first=10, ingested=now-timedelta(seconds=1))
    second = drain.process_existing(engine, now, output / "two", provider,
        max_attempts=3, process=block(engine, calls), runs_left=2)
    assert second["cohort_total"] == 8 and second["attempts_used"] == 3
    with pytest.raises(ValueError, match="Устаревший"):
        drain.process_existing(engine, now, output / "old", provider, runs_left=3)
    final = drain.process_existing(engine, now, output / "three", provider,
        max_attempts=3, process=block(engine, calls), runs_left=1)
    assert final["remaining"] == 0 and final["status_counts"]["blocked"] == 8
    assert len(calls) == len(set(calls)) == 8 and not {9, 10} & set(calls)
    assert campaign(engine)["completed_hops"] == [3, 2, 1]


def test_material_error_keeps_finite_future_retry_and_safe_report(state):
    engine, now, output = state
    seed(engine, now, 1)
    calls = []

    def failed(raw_id):
        calls.append(raw_id)
        raise RuntimeError("PRIVATE_BODY secret exception contents")

    report = drain.process_existing(engine, now, output / "failed", Provider(), process=failed)
    assert calls == [1] and report["attempts_used"] == 1 and report["remaining"] == 1
    assert report["errors"] and report["errors"][0]["error_type"] == "RuntimeError"
    assert datetime.fromisoformat(report["next_at"]) > datetime.now(UTC)+timedelta(minutes=4)
    assert "PRIVATE_BODY" not in (output / "failed" / "drain-report.json").read_text()
    assert report["next_delay_seconds"] == 60


def test_noop_callbacks_do_not_invent_claims_or_reset_material_budget(state):
    engine, now, output = state
    seed(engine, now, 1)
    report = drain.process_existing(engine, now, output / "noop", Provider(),
        max_attempts=30, process=lambda raw_id: {"raw_item_id": raw_id})
    assert report["attempts_used"] == 2
    assert report["process_callbacks"] == 3
    assert report["remaining_items"][0]["next_action"] == "fallback"
    assert datetime.fromisoformat(report["next_at"]) > datetime.now(UTC)+timedelta(minutes=29)


def test_wall_time_is_cooperative_and_does_not_block_unprocessed_items(state, monkeypatch):
    engine, now, output = state
    seed(engine, now, 3)
    clock = [0.0]
    monkeypatch.setattr(drain.time, "monotonic", lambda: clock[0])
    calls = []

    def process(raw_id):
        result = block(engine, calls)(raw_id)
        clock[0] = 2.0
        return result

    report = drain.process_existing(engine, now, output / "time", Provider(),
        max_seconds=1, process=process)
    assert report["attempts_used"] == 1 and report["remaining"] == 2
    assert report["status_counts"]["blocked"] == 1
    assert report["stop_reason"] == "time_budget_reached"


def test_delivery_uses_cohort_and_at_most_two_claims_in_entire_hop(state, monkeypatch):
    engine, now, output = state
    seed(engine, now, 8)
    sent = []

    def ready(raw_id):
        with Session(engine) as session:
            post = Post(city="belgrade", category="transport", status="ready",
                        title="Проверенный тест", body="PRIVATE_BODY", confidence=1)
            session.add(post)
            session.flush()
            session.add(EditorialRun(raw_item_id=raw_id, post_id=post.id,
                delivery_status="pending", outcome={"status": "ready", "ranking": {
                    "category": "transport"}, "_package": {}}, evidence={}))
            session.commit()
        return {"raw_item_id": raw_id}

    async def publish(engine, *, enabled, limit, only_raw_ids):
        assert enabled and len(only_raw_ids) <= limit <= 2
        result = []
        for raw_id in sorted(only_raw_ids):
            with Session(engine) as session:
                assert core.before_delivery_attempt(session, raw_id, now=datetime.now(UTC))
                session.commit()
                record = session.get(EditorialRun, raw_id)
                session.get(Post, record.post_id).telegram_message_id = 100+raw_id
                record.delivery_status = "sent"
                core.after_result(session, raw_id, now=datetime.now(UTC))
                session.commit()
            sent.append(raw_id)
            result.append(100+raw_id)
        return result

    monkeypatch.setattr(drain, "publish_pending", publish)
    report = drain.process_existing(engine, now, output / "delivery", Provider(),
        process=ready, send_enabled=True, max_attempts=10)
    assert report["delivery_attempts"] == report["delivered"] == len(sent) == 2
    assert report["status_counts"]["published"] == 2 and report["remaining"] == 6
    assert datetime.fromisoformat(report["next_at"]) <= datetime.now(UTC)


def test_large_remaining_report_is_bounded_without_losing_totals(state, monkeypatch):
    engine, now, output = state
    seed(engine, now, 1500)
    monkeypatch.setattr(drain, "run_cycle", lambda *a, **kw: {
        "status": "PRIMARY_OK", "processed_raw_item_ids": [], "remaining_count": 0})
    report = drain.process_existing(engine, now, output / "large", Provider())
    assert report["remaining"] == report["cohort_total"] == 1500
    assert len(report["remaining_items"]) == 100 and report["remaining_items_truncated"]
    assert (output / "large" / "drain-report.json").stat().st_size < 256*1024
    assert datetime.fromisoformat(report["next_at"]) > datetime.now(UTC)+timedelta(hours=5)


def test_invalid_terminal_publication_is_quarantined_not_hidden(state):
    engine, now, output = state
    seed(engine, now, 1)
    with Session(engine) as session:
        core.lifecycle_state(session, 1, now=now)
        row = session.scalar(select(SchedulerRun))
        row.details = {**row.details, "state": "published", "next_action": None,
                       "next_attempt_at": None}
        row.status = "published"
        session.commit()
    report = drain.process_existing(engine, now, output / "invalid", Provider())
    assert report["cohort_total"] == report["quarantine_count"] == report["remaining"] == 1
    assert report["status"] == "DRAIN_FAILED" and not report["needs_continue"]
    assert report["attempts_used"] == report["delivered"] == 0


def test_resumed_active_hop_keeps_already_consumed_claim_budget(state):
    engine, now, output = state
    seed(engine, now, 4)
    calls = []
    drain.process_existing(engine, now, output / "one", Provider(),
        max_attempts=2, process=block(engine, calls))
    with Session(engine) as session:
        row = session.scalar(select(SchedulerRun).where(SchedulerRun.task_type == drain.TASK_TYPE))
        row.details = {**row.details, "completed_hops": [], "active_hop": 1,
                       "hop_progress": {"runs_left": 1, "claim_start": 0,
                                        "delivery_claim_start": 0}}
        session.commit()
    resumed = drain.process_existing(engine, now, output / "resumed", Provider(),
        max_attempts=2, process=block(engine, calls))
    assert resumed["attempts_used"] == len(calls) == 2 and resumed["remaining"] == 2


def test_next_ai_phase_cannot_start_after_shared_wall_budget(state, monkeypatch):
    engine, now, output = state
    seed(engine, now, 1)
    clock, calls = [0.0], []
    monkeypatch.setattr(drain.time, "monotonic", lambda: clock[0])

    class Phases:
        def generate_json(self, **kwargs):
            calls.append(kwargs["task"])
            clock[0] = 2.0
            return "{}"

    def prepare(engine, raw_id, provider, output):
        for task in ("rank", "claims", "draft"):
            provider.generate_json(task=task, system_instruction="", prompt="", response_schema={})
        raise AssertionError("Дальнейшие этапы после лимита не допускаются")

    monkeypatch.setattr(drain, "prepare_reviewed", prepare)
    report = drain.process_existing(engine, now, output / "ai-budget", Phases(), max_seconds=1)
    assert calls == ["rank"] and report["attempts_used"] == 1
    assert report["remaining_items"][0]["next_action"] == "retry_prepare"
    assert datetime.fromisoformat(report["next_at"]) > datetime.now(UTC)+timedelta(minutes=4)


@pytest.mark.parametrize("options", [{"max_attempts": 0}, {"max_attempts": 31},
    {"max_seconds": 901}, {"runs_left": 201}, {"runs_left": 0}])
def test_invalid_limits_fail_before_any_processing(state, options):
    engine, now, output = state
    with pytest.raises(ValueError):
        drain.process_existing(engine, now, output / "invalid", Provider(), **options)
