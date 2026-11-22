import copy
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta

import pytest
from facts_check_helpers import reviewed_package_for
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.package import canonical_json
from relocant_media.domain.policy import SOURCE_REPLACED_REASON
from relocant_media.ops import backlog_drain as drain
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import material_lifecycle as core
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source, Submission
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


def test_advisory_provider_failure_keeps_drain_full_attempt_and_material_retry(state, monkeypatch):
    from relocant_media.ai.gemini_process import GeminiWorkerError

    engine, now, output = state
    seed(engine, now, 2)
    monkeypatch.setenv("EDITORIAL_GPT_FALLBACK_ENABLED", "true")
    calls, tasks = [], []

    class Unavailable:
        def generate_json(self, **kwargs):
            tasks.append(kwargs["task"])
            raise GeminiWorkerError("Синтетический временный сбой")

    def full(raw_id):
        calls.append(raw_id)
        raise GeminiWorkerError("Синтетический сбой полного ранжирования")

    report = drain.process_existing(engine, now, output / "provider-failure", Unavailable(),
                                    max_attempts=1, process=full)
    assert tasks == ["batch_preselect"] and len(calls) == 1
    assert report["attempts_used"] == 1 and report["remaining"] == 2
    with Session(engine) as session:
        state = core.lifecycle_state(session, calls[0], now=datetime.now(UTC))
        assert state["attempts"] == 1 and state["fallback_attempts"] == 0
        assert state["next_action"] == "retry_prepare" and state["state"] == "waiting"
        assert datetime.fromisoformat(state["next_attempt_at"]) > datetime.now(UTC)
        primary = list(session.scalars(select(SchedulerRun).where(
            SchedulerRun.task_type == "editorial_primary")))
        assert primary and all(row.details["batch_preselection"]["status"] == "fallback"
                               for row in primary)
        assert all("gpt_candidate_ids" not in row.details["batch_preselection"] for row in primary)
        assert primary[0].details["processing_errors"][0]["error_type"] == "GeminiWorkerError"


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
            payload = reviewed_package_for(session.get(RawItem, raw_id),
                session.get(Source, "official"), title=post.title, body=post.body, now=now)
            package_sha = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
            session.add(EditorialRun(raw_item_id=raw_id, post_id=post.id,
                delivery_status="pending", outcome={**payload["outcome"], "_package": payload,
                    "_package_sha256": package_sha},
                evidence={}))
            session.commit()
        return {"raw_item_id": raw_id}

    async def publish(engine, *, enabled, limit, only_raw_ids, scheduler_run_id=None):
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


def test_owner_close_preserves_terminal_decisions_budgets_history_and_frozen_scope(state):
    engine, now, output = state
    seed(engine, now, 3)
    drain._campaign(engine, now, 5)
    seed(engine, now, 1, first=4, ingested=now+timedelta(seconds=1))
    with Session(engine) as session:
        assert core.before_attempt(session, 1, now=now)
        core.after_error(session, 1, "TimeoutError", now=now)
        active = session.scalar(select(SchedulerRun).where(
            SchedulerRun.task_type == core.TASK_TYPE))
        active.details = {**active.details, "history": [{"step": index} for index in range(20)]}
        history = copy.deepcopy(active.details["history"])
        core.block_unsupported_repair(session, 2, now=now)
        blocked = core.lifecycle_state(session, 2, now=now)
        post = Post(city="belgrade", category="transport", status="ready",
                    title="Готово", body="Сохранённый текст", confidence=1,
                    telegram_message_id=101, published_at=now)
        session.add(post)
        session.flush()
        run = EditorialRun(raw_item_id=3, post_id=post.id, delivery_status="sent",
                           outcome={"status": "ready", "reason": "original_reason"}, evidence={})
        session.add(run)
        core.after_result(session, 3, now=now)
        published = core.lifecycle_state(session, 3, now=now)
        session.commit()
    report = drain.close_existing(engine, now, output / "close")
    assert report["remaining"] == 0 and not report["needs_continue"]
    assert report["mode"] == "close_existing" and report["closed_by_owner"]
    assert report["status_counts"] == {"published": 1, "blocked": 2}
    assert report["attempts_used"] == report["delivered"] == 0
    with Session(engine) as session:
        details = core.lifecycle_state(session, 1, now=now)
        assert details["reason"] == "owner_closed_backlog" and details["attempts"] == 1
        assert details["history"][:20] == history and len(details["history"]) == 21
        assert core.lifecycle_state(session, 2, now=now) == blocked
        assert core.lifecycle_state(session, 3, now=now) == published
        assert session.get(EditorialRun, 3).outcome["reason"] == "original_reason"
        assert session.get(RawItem, 4).body.startswith("PRIVATE_BODY")
        assert not session.scalar(select(SchedulerRun).where(
            SchedulerRun.task_key == "material-lifecycle:belgrade:4"))
    saved = campaign(engine)
    assert saved["completed_hops"] == [] and saved["initial_run_budget"] == 5
    assert saved["closed_by_owner"] and saved["cohort_ids"] == [1, 2, 3]
    repeated = drain.close_existing(engine, now, output / "repeat")
    assert repeated["replay"] and repeated["closed_count"] == 0
    with Session(engine) as session:
        assert core.lifecycle_state(session, 1, now=now)["history"] == details["history"]
    class Forbidden:
        def generate_json(self, **kwargs):
            pytest.fail("После ручной отмены модель не вызывается")
    stopped = drain.process_existing(engine, now, output / "stopped", Forbidden(), runs_left=200,
        process=lambda raw_id: pytest.fail("Закрытый cohort не возобновляется"), send_enabled=True)
    assert stopped["closed_by_owner"] and not stopped["needs_continue"]


@pytest.mark.parametrize("transport", ["uncertain", "sending", "staging"])
def test_owner_close_keeps_uncertain_transport_and_exact_owner_approval(state, transport):
    engine, now, output = state
    seed(engine, now, 3)
    drain._campaign(engine, now, 3)
    with Session(engine) as session:
        for raw_id in (1, 2, 3):
            core.lifecycle_state(session, raw_id, now=now)
            post = Post(city="belgrade", category="transport", status="human_review",
                        title="Точный черновик", body="Сохранить", confidence=1)
            session.add(post)
            session.flush()
            run = EditorialRun(raw_item_id=raw_id, post_id=post.id,
                delivery_status=transport if raw_id == 1 and transport != "staging" else "pending",
                staging_message_id=77 if raw_id == 1 and transport == "staging" else None,
                outcome={"status": "human_review", "reason": "requires_approval",
                         "_package_sha256": "a"*64,
                         **({"_rich_delivery": {"state": "uncertain"}} if raw_id == 1 else {})},
                evidence={})
            session.add(run)
        review = Submission(kind="owner_editorial_review", status="approved",
            telegram_user_id=1, telegram_message_id=50, payload={"raw_item_id": 2,
                "package_sha256": "a"*64, "owner_decision": "publish", "owner_chat_id": 1})
        session.add(review)
        session.commit()
        uncertain = copy.deepcopy(session.get(EditorialRun, 1).outcome)
        approved = copy.deepcopy(session.get(EditorialRun, 2).outcome)
        approval = copy.deepcopy(review.payload)
    report = drain.close_existing(engine, now, output / "close")
    assert report["remaining"] == 0 and report["requires_reconciliation_count"] == 1
    with Session(engine) as session:
        assert session.get(EditorialRun, 1).delivery_status == (
            "pending" if transport == "staging" else transport)
        assert session.get(EditorialRun, 1).staging_message_id == (
            77 if transport == "staging" else None)
        assert session.get(EditorialRun, 1).outcome == uncertain
        assert (core.lifecycle_state(session, 1, now=now)["reason"]
                == "delivery_reconciliation_required")
        assert session.get(EditorialRun, 2).outcome == approved
        assert session.get(Post, session.get(EditorialRun, 2).post_id).status == "human_review"
        saved = session.scalar(select(Submission))
        assert saved.status == "approved" and saved.payload == approval
        with pytest.raises(core.LifecycleBlockedError):
            core.ensure_not_terminal(session, 2, now=now)


def test_owner_close_includes_only_proven_post_cutoff_replacement(state):
    engine, now, output = state
    seed(engine, now, 1)
    drain._campaign(engine, now, 3)
    seed(engine, now, 1, first=2, ingested=now+timedelta(seconds=1))
    with Session(engine) as session:
        assert core.before_attempt(session, 1, now=now)
        session.add(EditorialRun(raw_item_id=1, delivery_status="pending", evidence={}, outcome={
            "status": "skipped", "reason": SOURCE_REPLACED_REASON, "replacement_raw_item_id": 2}))
        session.flush()
        core.inherit_lifecycle(session, 1, 2, now=now)
        session.commit()
    report = drain.close_existing(engine, now, output / "close")
    assert report["remaining"] == 0 and report["cohort_total"] == 1
    with Session(engine) as session:
        assert core.lifecycle_state(session, 2, now=now)["reason"] == "owner_closed_backlog"
        assert core.lifecycle_state(session, 1, now=now)["attempts"] == 1
        assert session.get(EditorialRun, 1).outcome["reason"] == SOURCE_REPLACED_REASON
        assert session.get(EditorialRun, 2) is None


@pytest.mark.parametrize("damage", ["missing_campaign", "hash", "alias", "replacement"])
def test_owner_close_validates_every_link_before_mutation(state, damage):
    engine, now, output = state
    seed(engine, now, 2)
    if damage != "missing_campaign":
        drain._campaign(engine, now, 3)
    if damage == "replacement":
        seed(engine, now, 1, first=3, ingested=now+timedelta(seconds=1))
    with Session(engine) as session:
        for raw_id in ((1, 2, 3) if damage == "replacement" else (1, 2)):
            core.lifecycle_state(session, raw_id, now=now)
        if damage == "hash":
            row = session.scalar(select(SchedulerRun).where(
                SchedulerRun.task_type == drain.TASK_TYPE))
            row.details = {**row.details, "cohort_sha256": "invalid"}
        elif damage == "alias":
            row = session.scalar(select(SchedulerRun).where(
                SchedulerRun.task_key == "material-lifecycle:belgrade:2"))
            row.details = {**row.details, "lifecycle_root_raw_id": 999}
        elif damage == "replacement":
            session.add(EditorialRun(raw_item_id=1, delivery_status="pending", evidence={},
                outcome={"status": "skipped", "reason": SOURCE_REPLACED_REASON,
                         "replacement_raw_item_id": 3}))
        session.commit()
        prior = {row.id: copy.deepcopy(row.details)
                 for row in session.scalars(select(SchedulerRun))}
    with pytest.raises(ValueError):
        drain.close_existing(engine, now, output / "invalid-close")
    with Session(engine) as session:
        assert {row.id: row.details for row in session.scalars(select(SchedulerRun))} == prior
    assert not (output / "invalid-close" / "drain-report.json").exists()


def test_owner_close_cli_does_not_construct_model_or_require_network_keys(state, monkeypatch):
    engine, now, output = state
    seed(engine, now, 1)
    drain._campaign(engine, now, 1)
    monkeypatch.setenv("DATABASE_URL", str(engine.url))
    for key in ("GEMINI_API_KEY", "GEMINI_MODEL", "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(drain, "IsolatedGeminiProvider", lambda *a, **k: pytest.fail("Нет модели"))
    monkeypatch.setattr(drain, "prepare_reviewed", lambda *a, **k: pytest.fail("Нет подготовки"))
    monkeypatch.setattr(drain, "publish_pending", lambda *a, **k: pytest.fail("Нет отправки"))
    monkeypatch.setattr(sys, "argv", ["backlog_drain", "--before", now.isoformat(),
        "--output", str(output / "cli-close"), "--close-existing", "--send-enabled"])
    drain.main()
    assert json.loads((output / "cli-close" / "drain-report.json").read_text())["remaining"] == 0


def test_owner_close_rolls_back_entire_cohort_if_a_write_fails(state, monkeypatch):
    engine, now, output = state
    seed(engine, now, 2)
    drain._campaign(engine, now, 3)
    calls = []
    close = core.close_by_owner

    def interrupted(session, raw_id, **kwargs):
        calls.append(raw_id)
        if len(calls) == 2:
            raise RuntimeError("Синтетическая ошибка сохранения")
        return close(session, raw_id, **kwargs)

    monkeypatch.setattr(core, "close_by_owner", interrupted)
    with pytest.raises(RuntimeError):
        drain.close_existing(engine, now, output / "interrupted-close")
    with Session(engine) as session:
        assert not list(session.scalars(select(SchedulerRun).where(
            SchedulerRun.task_type == core.TASK_TYPE)))
        row = session.scalar(select(SchedulerRun).where(SchedulerRun.task_type == drain.TASK_TYPE))
        assert "closed_by_owner" not in row.details
    assert not (output / "interrupted-close" / "drain-report.json").exists()
