"""Ограниченный разбор уже собранной очереди каноническим Actions-писателем.

Caller держит общую CAS-блокировку и сохраняет штатный снимок после завершения.
Здесь нет сбора источников, discovery, нового согласования владельца или второго writer.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from relocant_media.ai.batch_preselection import preselect_candidates
from relocant_media.ai.gemini_process import IsolatedGeminiProvider
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.ai.public_cache import PublicPreparationCache
from relocant_media.domain.city_profile import load_city_profile, load_queue_place_aliases
from relocant_media.domain.policy import SOURCE_REPLACED_REASON
from relocant_media.ops.candidates_cli import aware_datetime
from relocant_media.ops.primary_cycle import run_cycle
from relocant_media.ops.server_cycle import (
    ServerAI,
    media_sources,
    prepare_reviewed,
    publish_pending,
    queue_priorities,
)
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import material_lifecycle as core
from relocant_media.services.editorial_products import recent_publications
from relocant_media.services.gpt_fallback import pending_gpt_ids
from relocant_media.services.material_lifecycle_runtime import MaterialLifecycleRuntime
from relocant_media.services.published_watch import recent_source_load
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source
from relocant_media.storage.sqlite_schema import verify_sqlite_schema

CITY = "belgrade"
TASK_TYPE = "material_backlog_drain"
POLICY_VERSION = "existing-backlog-v1"
CLAIM_COUNTERS = ("attempts", "audit_attempts", "fallback_attempts")


class DrainBudgetReached(RuntimeError):
    """Следующая попытка остаётся штатной очереди; это не отказ материалу."""


class _BudgetProvider:
    """Общий срок снаружи кэша: после него не начинается следующий AI-этап."""

    def __init__(self, provider: StructuredAIProvider, deadline: float):
        self.provider, self.deadline = provider, deadline

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        if time.monotonic() >= self.deadline:
            raise DrainBudgetReached("Бюджет истёк до следующего вызова модели")
        return self.provider.generate_json(task=task, system_instruction=system_instruction,
                                          prompt=prompt, response_schema=response_schema)


def _utc() -> datetime:
    return datetime.now(UTC)


def _date(value: Any) -> datetime | None:
    try:
        return aware_datetime(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _digest(ids: list[int]) -> str:
    return hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode()).hexdigest()


def _root(records: dict[int, dict[str, Any]], raw_id: int) -> dict[str, Any] | None:
    seen: set[int] = set()
    while raw_id not in seen:
        seen.add(raw_id)
        state = records.get(raw_id)
        if state is None:
            return None
        parent = state.get("lifecycle_root_raw_id")
        if parent is None:
            return state
        if type(parent) is not int:
            return None
        raw_id = parent
    return None


def _view(engine: Engine, cohort: set[int], *, session: Session | None = None) -> dict[str, Any]:
    """Только технические поля; ни outcome, ни evidence целиком не экспортируются."""
    if session is None:
        with Session(engine) as reader:
            return _view(engine, cohort, session=reader)
    records: dict[int, dict[str, Any]] = {}
    for row in session.scalars(select(SchedulerRun).where(
            SchedulerRun.task_type == core.TASK_TYPE,
            SchedulerRun.city == CITY).order_by(SchedulerRun.id)):
        item_id = row.details.get("raw_item_id")
        if type(item_id) is int:
            records[item_id] = row.details
    rows = session.execute(select(
        EditorialRun.raw_item_id, EditorialRun.delivery_status,
        EditorialRun.outcome["reason"].as_string(),
        EditorialRun.outcome["replacement_raw_item_id"].as_integer(),
        Post.telegram_message_id,
    ).outerjoin(Post, Post.id == EditorialRun.post_id))
    runs = {row[0]: row for row in rows}
    scope = set(cohort)
    for raw_id in records:
        root = _root(records, raw_id)
        if root and root.get("raw_item_id") in cohort:
            scope.add(raw_id)
    states, claims, delivery_claims, seen_roots = {}, 0, 0, set()
    for raw_id in scope:
        state = _root(records, raw_id)
        if state and state.get("raw_item_id") not in seen_roots:
            seen_roots.add(state.get("raw_item_id"))
            claims += sum(state.get(key, 0) for key in CLAIM_COUNTERS)
            delivery_claims += state.get("delivery_attempts", 0)
        target, visited = raw_id, set()
        while target not in visited:
            visited.add(target)
            run = runs.get(target)
            if run is None or run[2] != SOURCE_REPLACED_REASON or type(run[3]) is not int:
                break
            # Замена разрешена только при доказанной принадлежности тому же root ledger.
            linked = _root(records, run[3])
            if state is None or linked is not state:
                break
            target = run[3]
        run = runs.get(target)
        published = bool(run and run[1] == "sent" and type(run[4]) is int and run[4] > 0)
        if published:
            status, reason = "published", "telegram_receipt_confirmed"
        elif state and state.get("state") == "blocked":
            status, reason = "blocked", state.get("reason", "terminal_blocked")
        elif state and state.get("state") in core.TERMINAL_STATES:
            status, reason = "untracked", "terminal_without_valid_receipt"
        else:
            status, reason = "active", str(state.get("reason")) if state else "not_enrolled"
        states[raw_id] = {"raw_id": raw_id, "status": status, "reason": reason,
                         "next_action": state.get("next_action") if state else None,
                         "next_at": state.get("next_attempt_at") if state else None,
                         "deadline_at": state.get("deadline_at") if state else None,
                         "fallback_deadline_at": (state.get("fallback_deadline_at")
                                                  if state else None)}
    return {"states": states, "scope": scope, "claims": claims,
            "delivery_claims": delivery_claims}


def _validated_campaign(row: SchedulerRun, cutoff: datetime) -> dict[str, Any]:
    data = row.details
    ids = data.get("cohort_ids")
    budget = data.get("initial_run_budget")
    if (data.get("schema_version") != 1 or data.get("policy_version") != POLICY_VERSION
            or data.get("cutoff") != cutoff.isoformat() or not isinstance(ids, list)
            or any(type(raw_id) is not int or raw_id <= 0 for raw_id in ids)
            or ids != sorted(set(ids)) or data.get("cohort_sha256") != _digest(ids)
            or type(budget) is not int or not 1 <= budget <= 200):
        raise ValueError("Повреждена фиксированная когорта разбора")
    completed = data.get("completed_hops")
    if (not isinstance(completed, list) or len(completed) > budget
            or any(type(hop) is not int for hop in completed)
            or completed != list(range(budget, budget-len(completed), -1))):
        raise ValueError("Повреждён сохранённый бюджет продолжений")
    return dict(data)


def _campaign(engine: Engine, cutoff: datetime, runs_left: int) -> tuple[int, dict[str, Any]]:
    key = f"material-backlog-drain:{CITY}:{cutoff.isoformat()}"
    with Session(engine) as session:
        row = session.scalar(select(SchedulerRun).where(
            SchedulerRun.task_type == TASK_TYPE, SchedulerRun.task_key == key,
        ).order_by(SchedulerRun.id.desc()).limit(1))
        if row is None:
            initial_ids = set(session.scalars(select(RawItem.id).join(Source).where(
                Source.city == CITY, RawItem.ingested_at <= cutoff)))
            view = _view(engine, initial_ids)
            cohort = sorted(raw_id for raw_id in initial_ids
                            if view["states"][raw_id]["status"] not in {"published", "blocked"})
            quarantined = sum(view["states"][raw_id]["status"] == "untracked" for raw_id in cohort)
            row = SchedulerRun(task_key=key, task_type=TASK_TYPE, city=CITY,
                scheduled_for=cutoff, attempt=1, started_at=_utc(), status="running",
                details={"schema_version": 1, "policy_version": POLICY_VERSION,
                         "cutoff": cutoff.isoformat(), "cohort_ids": cohort,
                         "cohort_sha256": _digest(cohort), "initial_run_budget": runs_left,
                         "quarantine_count": quarantined, "completed_hops": []})
            session.add(row)
            session.commit()
        data = _validated_campaign(row, cutoff)
        if data.get("closed_by_owner") is True:
            return row.id, data
        completed = data["completed_hops"]
        expected = data["initial_run_budget"]-len(completed)
        if completed and runs_left == completed[-1]:
            return row.id, {**data, "replay": True}
        if runs_left != expected:
            raise ValueError("Устаревший или пропущенный шаг продолжения")
        if data.get("active_hop") not in {None, runs_left}:
            raise ValueError("Другой шаг разбора ещё не завершён")
        return row.id, dict(data)


def _store(engine: Engine, campaign_id: int, *, campaign_status: str | None = None,
           finished_at: datetime | None = None, **fields: Any) -> None:
    with Session(engine) as session:
        row = session.get(SchedulerRun, campaign_id)
        if row is None:
            raise ValueError("Исчезла фиксированная когорта")
        row.details = {**row.details, **fields}
        if campaign_status is not None:
            row.status = campaign_status
        if finished_at is not None:
            row.finished_at = finished_at
        session.commit()


def _write_report(output: Path, report: dict[str, Any]) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    (output / "drain-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def _close_targets(session: Session, cohort: set[int], cutoff: datetime) -> list[int]:
    """Полностью проверить область отмены до первой записи, включая связи замен."""
    records: dict[int, SchedulerRun] = {}
    for row in session.scalars(select(SchedulerRun).where(
        SchedulerRun.task_type == core.TASK_TYPE, SchedulerRun.city == CITY,
    ).order_by(SchedulerRun.id)):
        raw_id = row.details.get("raw_item_id")
        if type(raw_id) is int:
            records[raw_id] = row
    scope, roots, edges = set(cohort), {}, {}
    pending = list(cohort)

    def objects(raw_id: int) -> tuple[RawItem, EditorialRun | None]:
        raw = session.get(RawItem, raw_id)
        source = session.get(Source, raw.source_id) if raw else None
        if raw is None or source is None or source.city != CITY:
            raise ValueError("Материал фиксированной когорты или его источник отсутствует")
        return raw, session.get(EditorialRun, raw_id)

    while pending:
        raw_id = pending.pop()
        if raw_id in roots:
            continue
        raw, run = objects(raw_id)
        if raw_id in cohort and raw.ingested_at > cutoff:
            raise ValueError("Материал не принадлежит исходному cutoff")
        record = records.get(raw_id)
        root_id = raw_id
        if record is not None:
            data = record.details
            if (record.task_key != f"material-lifecycle:{CITY}:{raw_id}"
                    or data.get("policy_version") != core.POLICY_VERSION):
                raise ValueError("Повреждена запись жизненного цикла")
            if "lifecycle_root_raw_id" in data:
                root_id = data["lifecycle_root_raw_id"]
                parent = records.get(root_id) if type(root_id) is int else None
                _, original = objects(root_id) if type(root_id) is int else (None, None)
                if (type(root_id) is not int or root_id <= 0 or root_id == raw_id
                        or parent is None or "lifecycle_root_raw_id" in parent.details
                        or parent.details.get("replacement_raw_item_id") != raw_id
                        or original is None
                        or original.outcome.get("reason") != SOURCE_REPLACED_REASON
                        or original.outcome.get("replacement_raw_item_id") != raw_id):
                    raise ValueError("Повреждена связь первоисточника с жизненным циклом")
                if root_id not in scope:
                    scope.add(root_id)
                    pending.append(root_id)
            else:
                if (data.get("schema_version") != 1
                        or data.get("state") not in core.TERMINAL_STATES
                        and not core.valid_owner_close_state(data)
                        or not isinstance(data.get("history", []), list)):
                    raise ValueError("Повреждено состояние жизненного цикла")
                replacement = data.get("replacement_raw_item_id")
                if replacement is not None and (run is None
                        or run.outcome.get("reason") != SOURCE_REPLACED_REASON
                        or run.outcome.get("replacement_raw_item_id") != replacement):
                    raise ValueError("Отсутствует доказанная замена первоисточника")
        roots[raw_id] = root_id
        if run is not None and run.outcome.get("reason") == SOURCE_REPLACED_REASON:
            target = run.outcome.get("replacement_raw_item_id")
            if type(target) is not int or target <= 0 or target == raw_id:
                raise ValueError("Повреждена редакционная связь первоисточника")
            objects(target)
            edges[raw_id] = target
            if target not in scope:
                scope.add(target)
                pending.append(target)
        for child_id, child in records.items():
            if child.details.get("lifecycle_root_raw_id") == raw_id and child_id not in scope:
                scope.add(child_id)
                pending.append(child_id)
    for original_id, replacement in edges.items():
        linked_parent, linked_child = records.get(original_id), records.get(replacement)
        if (linked_parent is None or linked_child is None
                or roots[original_id] != roots[replacement]
                or linked_parent.details.get("replacement_raw_item_id") != replacement
                or linked_child.details.get("lifecycle_root_raw_id") != original_id):
            raise ValueError("Замена не принадлежит доказанному общему жизненному циклу")
    for raw_id in scope:
        visited: set[int] = set()
        target = raw_id
        while target in edges:
            if target in visited:
                raise ValueError("Цикл замен первоисточника")
            visited.add(target)
            target = edges[target]
    targets = [raw_id for raw_id in scope if raw_id not in edges]
    return sorted(targets)


def close_existing(engine: Engine, cutoff: datetime, output: Path) -> dict[str, Any]:
    """Атомарно снять остаток существующей кампании по решению владельца, без AI и сети."""
    cutoff = aware_datetime(cutoff.isoformat())
    now = _utc()
    if cutoff > now:
        raise ValueError("Cutoff не должен быть в будущем")
    verify_sqlite_schema(engine)
    key = f"material-backlog-drain:{CITY}:{cutoff.isoformat()}"
    with Session(engine) as session, session.begin():
        lock_editorial_city(session, CITY)
        row = session.scalar(select(SchedulerRun).where(
            SchedulerRun.task_type == TASK_TYPE, SchedulerRun.task_key == key,
            SchedulerRun.city == CITY,
        ).order_by(SchedulerRun.id.desc()).limit(1).with_for_update())
        if row is None:
            raise ValueError("Нет существующей фиксированной кампании с этим cutoff")
        campaign = _validated_campaign(row, cutoff)
        cohort = set(campaign["cohort_ids"])
        targets = _close_targets(session, cohort, cutoff)
        before = _view(engine, cohort | set(targets), session=session)
        closed = 0
        for raw_id in targets:
            state = before["states"].get(raw_id)
            if state is None or state["status"] not in {"published", "blocked"}:
                core.close_by_owner(session, raw_id, now=now)
                closed += 1
        session.flush()
        final = _view(engine, cohort, session=session)
        states = [final["states"][raw_id] for raw_id in sorted(cohort)]
        remaining = sum(item["status"] not in {"published", "blocked"} for item in states)
        if remaining:
            raise ValueError("Не все материалы фиксированной когорты удалось закрыть")
        counts = Counter(item["status"] for item in states)
        reconciliation = 0
        for raw_id in targets:
            run = session.get(EditorialRun, raw_id)
            post = session.get(Post, run.post_id) if run and run.post_id else None
            confirmed = bool(run and run.delivery_status == "sent" and post
                             and type(post.telegram_message_id) is int
                             and post.telegram_message_id > 0)
            unsafe = bool(run and (run.delivery_status in {"sending", "uncertain", "sent"}
                          or run.staging_message_id is not None) or post and (
                          post.telegram_message_id is not None or post.published_at is not None))
            state = final["states"].get(raw_id)
            reconciliation += bool(unsafe and not confirmed or state
                                   and state["reason"] == "delivery_reconciliation_required")
        replay = campaign.get("closed_by_owner") is True
        report = {"status": "DRAIN_OK", "mode": "close_existing", "closed_by_owner": True,
                  "observed_at": now.isoformat(), "cutoff": cutoff.isoformat(),
                  "cohort_total": len(cohort), "cohort_sha256": campaign["cohort_sha256"],
                  "remaining": 0, "needs_continue": False, "next_at": None,
                  "next_delay_seconds": 0, "attempts_used": 0, "delivered": 0,
                  "closed_count": closed, "replay": replay,
                  "requires_reconciliation": bool(reconciliation),
                  "requires_reconciliation_count": reconciliation,
                  "status_counts": {status: counts[status] for status in ("published", "blocked")},
                  "stop_reason": "closed_by_owner"}
        row.details = {**campaign, "closed_by_owner": True,
                       "closed_by_owner_at": campaign.get("closed_by_owner_at", now.isoformat()),
                       "close_reason": "owner_closed_backlog", "last_report": report}
        row.status, row.finished_at = "closed_by_owner", now
    return _write_report(output, report)


def process_existing(
    engine: Engine, cutoff: datetime, output: Path, provider: StructuredAIProvider,
    max_attempts: int = 10, max_seconds: int = 300,
    process: Callable[[int], dict[str, Any]] | None = None,
    send_enabled: bool = False, runs_left: int = 1,
) -> dict[str, Any]:
    """Разобрать прежнюю когорту, не меняя индивидуальных бюджетов и разрешений."""
    cutoff = aware_datetime(cutoff.isoformat())
    if (type(max_attempts) is not int or not 1 <= max_attempts <= 30
            or type(max_seconds) is not int or not 1 <= max_seconds <= 900
            or type(runs_left) is not int or not 1 <= runs_left <= 200
            or type(send_enabled) is not bool):
        raise ValueError("Лимиты: attempts 1..30, seconds 1..900, runs-left 1..200")
    verify_sqlite_schema(engine)
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    provider = _BudgetProvider(provider, started+max_seconds)
    campaign_id, campaign = _campaign(engine, cutoff, runs_left)
    if campaign.get("closed_by_owner") is True:
        return _write_report(output, {**campaign["last_report"], "status": "DRAIN_OK",
                                     "replay": True, "needs_continue": False,
                                     "next_delay_seconds": 0})
    if campaign.get("replay"):
        previous = campaign.get("last_report", {})
        return _write_report(output, {**previous, "status": "DRAIN_OK", "replay": True,
                                     "needs_continue": False, "next_delay_seconds": 0})
    cohort = set(campaign["cohort_ids"])
    initial = _view(engine, cohort)
    progress = campaign.get("hop_progress")
    if progress is None:
        progress = {"runs_left": runs_left, "claim_start": initial["claims"],
                    "delivery_claim_start": initial["delivery_claims"]}
        _store(engine, campaign_id, active_hop=runs_left, hop_progress=progress)
    if (not isinstance(progress, dict) or progress.get("runs_left") != runs_left
            or type(progress.get("claim_start")) is not int
            or type(progress.get("delivery_claim_start")) is not int):
        raise ValueError("Повреждён бюджет текущего шага")
    lifecycle = MaterialLifecycleRuntime(engine, CITY, provider)
    profile = load_city_profile(CITY)
    errors: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
    invoked = 0
    sent: list[int] = []
    due: list[int] = []
    last_remaining = 0

    def used() -> tuple[int, int]:
        view = _view(engine, cohort)
        return (view["claims"]-progress["claim_start"],
                view["delivery_claims"]-progress["delivery_claim_start"])

    def scope() -> set[int]:
        return set(_view(engine, cohort)["scope"])

    def reconcile() -> tuple[list[int], set[int]]:
        nonlocal due
        allowed = scope()
        with Session(engine) as session:
            sweep = core.sweep_materials(session, city=CITY, now=_utc(), raw_ids=allowed)
            session.commit()
        view = _view(engine, cohort)
        # Новые prepare проходят обычный selector и batch preselection, а не FIFO ID.
        due = [*sweep.rejection_audit_ids, *(raw_id for raw_id in sweep.due_ids
               if view["states"][raw_id]["next_action"] != "prepare")]
        return due, set(sweep.excluded_ids)

    def bounded_process(raw_id: int) -> dict[str, Any]:
        nonlocal invoked
        claims, _ = used()
        if claims >= max_attempts or time.monotonic()-started >= max_seconds:
            raise DrainBudgetReached("Бюджет шага исчерпан, материал остаётся в очереди")
        if raw_id not in scope():
            raise ValueError("Попытка за пределами фиксированной когорты")
        invoked += 1
        return lifecycle.process(raw_id, process or (lambda selected: prepare_reviewed(
            engine, selected, lifecycle.provider_for(selected), output)), retry_refuted=False)

    def preselect(items: list[dict[str, Any]], limit: int
                  ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if time.monotonic()-started >= max_seconds:
            raise DrainBudgetReached("Истёк срок шага до предварительного отбора")
        with Session(engine) as session:
            recent = recent_publications(session, CITY, now=_utc())
        return preselect_candidates(items, provider, profile=profile, limit=limit, now=_utc(),
            recent_publications=[{"id": str(row["id"]), "title": row["title"], "body": row["body"]}
                                 for row in recent[:20]])

    def deferred(session: Session) -> set[int]:
        return pending_gpt_ids(session, now=_utc()) & scope()

    def source_load() -> dict[str, int]:
        with Session(engine) as session:
            return recent_source_load(session, now=_utc())

    stop_reason = "no_actionable_materials"
    try:
        reconcile()
        while time.monotonic()-started < max_seconds:
            claims_before, deliveries_before = used()
            if claims_before >= max_attempts:
                stop_reason = "attempt_budget_reached"
                break
            cycle_output = output / f"cycle-{len(cycles)+1:03d}-{time.time_ns()}"
            result = run_cycle(engine, city=CITY, output=cycle_output, now=_utc(),
                ingest=lambda: {"failures": 0, "raw_items_written": 0, "review_required": 0},
                process=bounded_process, limit=min(5, max_attempts-claims_before),
                allowed_items=scope, lifecycle_due=reconcile, lifecycle_retry_slots=5,
                close_window=False,
                lifecycle_fallback_items=lambda: {
                    raw_id for raw_id, state in _view(engine, cohort)["states"].items()
                    if state["next_action"] == "fallback"},
                preselect=preselect, refresh_until=True, newest_first=True,
                locality_aliases=profile.geography.city_aliases,
                local_place_aliases=load_queue_place_aliases(CITY),
                recent_source_counts=source_load, candidate_priorities=lambda: queue_priorities(
                    {}, media_sources()), deferred_items=deferred, continue_on_error=True,
                defer_failed_batch=os.environ.get("EDITORIAL_GPT_FALLBACK_ENABLED") == "true")
            cycles.append({"status": result.get("status"), "run_id": result.get("run_id")})
            last_remaining = result.get("remaining_count", 0)
            cycle_errors = [error for error in result.get("processing_errors", [])
                            if error.get("error_type") != "DrainBudgetReached"]
            errors.extend(cycle_errors)
            if result.get("error_type") and result["error_type"] != "DrainBudgetReached":
                errors.append({"error_type": result["error_type"], "stage": "primary_cycle"})
            claims_after, deliveries_after = used()
            if send_enabled and deliveries_after < 2 and time.monotonic()-started < max_seconds:
                ready = sorted(raw_id for raw_id, state in _view(engine, cohort)["states"].items()
                               if state["status"] == "active" and state["next_action"] == "deliver"
                               and (_date(state["next_at"]) or _utc()) <= _utc())
                if ready:
                    slots = 2-deliveries_after
                    sent.extend(asyncio.run(publish_pending(engine, enabled=True, limit=slots,
                        only_raw_ids=set(ready[:slots]))))
            if result.get("status") in {"PRIMARY_BUSY", "PRIMARY_FAILED"}:
                stop_reason = str(result["status"]).lower()
                break
            if claims_after == claims_before and used()[1] == deliveries_before:
                stop_reason = "no_actionable_materials"
                break
        else:
            stop_reason = "time_budget_reached"
    except Exception as error:
        errors.append({"error_type": type(error).__name__, "stage": "drain"})
        stop_reason = "drain_error"
    reconcile()
    final = _view(engine, cohort)
    remaining_items = [final["states"][raw_id] for raw_id in sorted(cohort)
                       if final["states"][raw_id]["status"] not in {"published", "blocked"}]
    now = _utc()
    dates = [_date(item["next_at"]) for item in remaining_items]
    future = [value for value in dates if value is not None and value > now]
    attempts_used, delivery_attempts = used()
    delivery_due = (send_enabled and delivery_attempts >= 2
                    and any(item["next_action"] == "deliver" for item in remaining_items))
    actual_due = bool(due) or delivery_due or last_remaining > 0 or (
        not cycles and stop_reason in {"attempt_budget_reached", "time_budget_reached"})
    next_at: datetime | None
    if remaining_items and actual_due:
        next_at = now
    else:
        # Selector/dedup может законно оставить prepare; не делаем пустые циклы до дедлайна.
        deadlines = [_date(item[key]) for item in remaining_items
                     for key in ("deadline_at", "fallback_deadline_at")]
        next_at = min([*future, *(value for value in deadlines if value and value > now)],
                      default=now+timedelta(seconds=60)) if remaining_items else None
    status_counts = Counter(final["states"][raw_id]["status"] for raw_id in cohort)
    quarantine = [item for item in remaining_items if item["status"] == "untracked"]
    only_quarantine = bool(remaining_items) and len(quarantine) == len(remaining_items)
    report = {"status": "DRAIN_FAILED" if only_quarantine or stop_reason in {
                  "drain_error", "primary_failed"}
              else "DRAIN_OK",
        "observed_at": now.isoformat(), "cutoff": cutoff.isoformat(), "cohort_total": len(cohort),
        "cohort_sha256": campaign["cohort_sha256"], "runs_left": runs_left, "replay": False,
        "attempts_used": attempts_used, "attempt_limit": max_attempts,
        "process_callbacks": invoked, "delivery_attempts": delivery_attempts,
        "delivered": len(sent), "send_enabled": send_enabled,
        "status_counts": {status: status_counts[status]
                          for status in ("published", "blocked", "active", "untracked")},
        "remaining": len(remaining_items), "remaining_items": remaining_items[:100],
        "remaining_items_truncated": len(remaining_items) > 100,
        "quarantine_count": len(quarantine), "requires_reconciliation": bool(quarantine),
        "requires_reconciliation_count": len(quarantine),
        "next_at": next_at.isoformat() if next_at else None,
        "needs_continue": bool(remaining_items) and not only_quarantine,
        "next_delay_seconds": min(60, max(0,
            math.ceil((next_at-now).total_seconds()))) if next_at else 0,
        "elapsed_seconds": round(time.monotonic()-started, 3), "stop_reason": stop_reason,
        "errors": errors, "cycles": cycles}
    _store(engine, campaign_id, completed_hops=[*campaign["completed_hops"], runs_left],
           active_hop=None, hop_progress=None, last_report=report,
           campaign_status=("success" if not remaining_items else "failed" if only_quarantine
                            else "stopped" if runs_left == 1 else "waiting"),
           finished_at=now if not remaining_items or runs_left == 1 or only_quarantine else None,
           stopped_by_budget=bool(remaining_items) and runs_left == 1)
    return _write_report(output, report)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=aware_datetime)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--max-attempts", default=10, type=int)
    parser.add_argument("--max-seconds", default=300, type=int)
    parser.add_argument("--runs-left", default=1, type=int)
    parser.add_argument("--send-enabled", action="store_true")
    parser.add_argument("--close-existing", action="store_true")
    args = parser.parse_args()
    engine = None
    try:
        url = make_url(os.environ["DATABASE_URL"])
        if (url.get_backend_name() != "sqlite" or not url.database
                or not Path(url.database).is_file() or url.query):
            raise ValueError("Нужен существующий канонический локальный SQLite")
        engine = create_engine(url)
        args.output.mkdir(parents=True, exist_ok=True)
        if args.close_existing:
            report = close_existing(engine, args.before, args.output)
            print(json.dumps({"status": report["status"], "needs_continue": False}))
            return
        budget_ends = time.monotonic()+args.max_seconds
        provider = PublicPreparationCache(ServerAI(IsolatedGeminiProvider(
            os.environ["GEMINI_API_KEY"], model=os.environ["GEMINI_MODEL"],
            remaining_seconds=lambda: budget_ends-time.monotonic()),
            audit_path=args.output / "ai-calls.jsonl", provider_name="gemini",
            model_id=os.environ["GEMINI_MODEL"]), Path(url.database).parent / "ai-cache",
            model_id=os.environ["GEMINI_MODEL"] + ":server-policy-v7-local-venue-style",
            public_news_only=True)
        report = process_existing(engine, args.before, args.output, provider,
            max_attempts=args.max_attempts, max_seconds=args.max_seconds,
            send_enabled=args.send_enabled, runs_left=args.runs_left)
    except Exception as error:
        # Строки исключений и переменные окружения не попадают в вывод.
        parser.exit(1, f"Разбор остановлен: {type(error).__name__}.\n")
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"status": report["status"], "needs_continue": report["needs_continue"]}))
    if report["status"] == "DRAIN_FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
