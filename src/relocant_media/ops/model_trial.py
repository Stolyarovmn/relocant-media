"""Read-only model trial (#312): compare candidate draft/review models on published posts.

The trial restores the verified canonical snapshot into a private working copy, takes the
latest published posts with stored reviewed packages and runs the current and the candidate
model bundles on the same items: a new draft and a fresh review of the published draft.
Nothing is written back to canonical state, no Telegram API is used, and every package linked
to a reader submission is excluded because free-tier model input may be used by the provider.
The uploaded file is an anonymized summary: counts, error types, time and sizes per model;
per-item rows stay in memory of the runner and are never written or uploaded.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.diagnostics import AI_PHASES
from relocant_media.ai.independent_reviews import reviewed_package
from relocant_media.ai.model_roles import RoleRoutedProvider, role_for, role_models
from relocant_media.ai.models import EditorialOutcome, PublicationStatus
from relocant_media.ai.package import EditorialPackage, PackageOutcome
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.storage.models import EditorialRun, Post, Submission

MAX_ITEMS = 15
_MODEL = r"[a-z0-9][a-z0-9.\-]{1,63}"
# The trial shares the API key quota with the working newsroom. It waits out a
# server-requested pause at most once per call and within one budget for the whole run;
# when a pause does not fit, it stops instead of competing with the newsroom for quota.
WAIT_BUDGET_SECONDS = 600.0
MAX_PAUSE_SECONDS = 120.0
INVALID_ERROR_SHARE = 0.5
_QUOTA_PHASES = frozenset({"http_error_429", "quota_cooldown"})


@dataclass
class CallLog:
    calls: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class WaitBudget:
    """Shared pause budget of one run; ``stopped`` ends the run after the current item."""

    remaining: float = WAIT_BUDGET_SECONDS
    max_pause: float = MAX_PAUSE_SECONDS
    sleep: Callable[[float], None] = time.sleep
    stopped: bool = False


class TrialStopped(RuntimeError):
    """The quota pause did not fit the budget; no further model calls are made."""


def _phase(error: BaseException) -> str:
    phase = getattr(error, "phase", None)
    return phase if isinstance(phase, str) and phase in AI_PHASES else "unknown"


def _retry_seconds(error: BaseException) -> float | None:
    quota = getattr(error, "quota", None)
    delay = quota.get("retry_seconds") if isinstance(quota, dict) else None
    return float(delay) if isinstance(delay, int | float) and delay >= 0 else None


class TimedProvider:
    """Record bundle, task, model, duration, sizes and success of each call.

    Prompt and answer text are never kept: only their lengths, as a cost proxy. The error
    record holds the exception class and a phase from the closed ``AI_PHASES`` list.
    """

    def __init__(self, inner: RoleRoutedProvider, log: CallLog, bundle: str,
                 budget: WaitBudget | None = None) -> None:
        self._inner, self._log, self._bundle = inner, log, bundle
        self._budget = budget if budget is not None else WaitBudget()

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        try:
            return self._attempt(task, system_instruction, prompt, response_schema, False)
        except TrialStopped:
            raise
        except Exception as error:
            if _phase(error) not in _QUOTA_PHASES:
                raise
            delay = _retry_seconds(error)
            budget = self._budget
            if delay is None or delay > min(budget.max_pause, budget.remaining):
                budget.stopped = True
                raise TrialStopped("quota pause does not fit the trial budget") from None
            budget.remaining -= delay
            budget.sleep(delay)
            return self._attempt(task, system_instruction, prompt, response_schema, True)

    def _attempt(self, task: str, system_instruction: str, prompt: str,
                 response_schema: dict[str, Any], retry: bool) -> str:
        if self._budget.stopped:
            raise TrialStopped("trial stopped to keep the newsroom quota")
        started = time.monotonic()
        entry: dict[str, Any] = {"bundle": self._bundle, "task": task, "role": role_for(task),
                                 "model": self._inner.model_for(task),
                                 "input_chars": len(system_instruction) + len(prompt),
                                 "output_chars": 0, "retry": retry}
        try:
            result = self._inner.generate_json(
                task=task, system_instruction=system_instruction, prompt=prompt,
                response_schema=response_schema)
            entry["output_chars"] = len(result)
        except Exception as error:
            entry["error_type"] = type(error).__name__
            entry["phase"] = _phase(error)
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - started, 2)
            self._log.calls.append(entry)
        return result


def validate_models(models: dict[str, str]) -> dict[str, str]:
    for role, name in models.items():
        if not re.fullmatch(_MODEL, name):
            raise ValueError(f"invalid {role} model name")
    return models


def reader_linked_raw_ids(session: Session) -> set[int]:
    """Every raw item that any reader submission points to, whatever its source."""
    linked: set[int] = set()
    payload: Any
    for payload in session.scalars(select(Submission.payload)):
        values = payload.get("raw_item_ids", []) if isinstance(payload, dict) else []
        if isinstance(values, list):
            linked.update(value for value in values if type(value) is int)
    return linked


def published_packages(session: Session, limit: int) -> list[tuple[Post, EditorialPackage]]:
    """Latest sent posts with a stored reviewed package, without any reader material.

    A package is skipped whole when its run carries a reader submission id or when its
    primary item or any event document is linked from a reader submission.
    """
    if not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"limit must be between 1 and {MAX_ITEMS}")
    reader_ids = reader_linked_raw_ids(session)
    rows = session.execute(
        select(EditorialRun, Post).join(Post, EditorialRun.post_id == Post.id)
        .where(EditorialRun.delivery_status == "sent", Post.telegram_message_id.is_not(None))
        .order_by(Post.published_at.desc()).limit(limit * 4)).all()
    result: list[tuple[Post, EditorialPackage]] = []
    for run, post in rows:
        outcome = run.outcome or {}
        if ("_reader_submission_id" in outcome or "_package" not in outcome
                or run.raw_item_id in reader_ids):
            continue
        try:
            package = EditorialPackage.model_validate_json(json.dumps(outcome["_package"]))
        except ValueError:
            continue
        if package.version != 2 or package.event is None:
            continue
        members = {package.raw_item_id, *(doc.raw_item_id for doc in package.event.documents)}
        if members & reader_ids:
            continue
        result.append((post, package))
        if len(result) == limit:
            break
    return result


def _verdicts(audit: list[dict[str, Any]]) -> dict[str, Any]:
    """Verdict and flag count per review role; reasons and flag text stay out."""
    result: dict[str, Any] = {}
    for entry in audit:
        role = str(entry.get("role"))
        flags = entry.get("flags") or []
        result[role] = {"verdict": entry.get("verdict"),
                        "flags": len(flags) if isinstance(flags, list) else 0}
    return result


def _placeholders(package: EditorialPackage) -> EditorialPackage:
    return package.model_copy(update={"reviews": tuple(review.model_copy(update={
        "verdict": "hold", "score": 0, "flags": (), "live_source_checked": False,
        "facts_check": None}) for review in package.reviews)})


def _review(package: EditorialPackage, documents: tuple[Any, ...], when: datetime,
            provider: StructuredAIProvider) -> dict[str, Any]:
    """Both independent reviews; a review failure is recorded and counts as not approved."""
    audit: list[dict[str, Any]] = []
    result: dict[str, Any] = {}
    try:
        reviewed_package(_placeholders(package), provider, lambda: documents, now=when,
                         audit=audit.append)
    except Exception as error:
        result["error_type"] = type(error).__name__
    result["reviews"] = _verdicts(audit)
    result["double_approval"] = ("error_type" not in result and bool(audit) and all(
        entry.get("verdict") == "approve" for entry in audit))
    return result


def _with_new_draft(package: EditorialPackage, outcome: EditorialOutcome) -> EditorialPackage:
    """The published package carrying the bundle's own outcome and draft."""
    new_outcome = PackageOutcome.model_validate_json(outcome.model_dump_json())
    presentation = package.presentation
    if presentation is not None:
        from relocant_media.services.native_map import bind_native_map_draft

        presentation = bind_native_map_draft(presentation, new_outcome.draft)
    return package.model_copy(update={"outcome": new_outcome, "presentation": presentation})


def trial_bundle(package: EditorialPackage, when: datetime, provider: StructuredAIProvider,
                 log: CallLog) -> dict[str, Any]:
    """One model bundle on one published package.

    ``published`` compares reviewers only: both reviews of the already published draft.
    ``full`` is the whole bundle: its own new draft and both reviews of that draft; a
    generation failure or a draft that is not ready counts as not approved.
    """
    first = len(log.calls)
    documents = tuple(package.event.documents) if package.event is not None else ()
    result: dict[str, Any] = {}
    outcome: EditorialOutcome | None = None
    try:
        outcome = EditorialPipeline(provider, city_profile=load_city_profile(package.city)).run(
            city=package.city, documents=documents, now=when)
        result["draft_status"] = outcome.status.value
    except Exception as error:
        result["draft_status"] = "error"
        result["draft_error_type"] = type(error).__name__
    result["published"] = _review(package, documents, when, provider)
    full: dict[str, Any] = {"double_approval": False, "reviews": {}}
    if (outcome is not None and outcome.status is PublicationStatus.READY
            and outcome.draft is not None):
        try:
            full = _review(_with_new_draft(package, outcome), documents, when, provider)
        except Exception as error:
            full["error_type"] = type(error).__name__
    result["full"] = full
    calls = log.calls[first:]
    result["seconds"] = round(sum(call["seconds"] for call in calls), 2)
    result["calls"] = len(calls)
    result["call_errors"] = sum(1 for call in calls if "error_type" in call)
    result["input_chars"] = sum(call["input_chars"] for call in calls)
    result["output_chars"] = sum(call["output_chars"] for call in calls)
    return result


def trial_item(post: Post, package: EditorialPackage,
               providers: dict[str, StructuredAIProvider], log: CallLog) -> dict[str, Any]:
    assert package.event is not None
    when = post.published_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    item: dict[str, Any] = {"raw_item_id": package.raw_item_id,
                            "category": package.outcome.ranking.category}
    for bundle, provider in providers.items():
        item[bundle] = trial_bundle(package, when, provider, log)
    facts = {bundle: item[bundle]["published"]["reviews"].get("facts", {}).get("verdict")
             for bundle in providers}
    item["facts_verdicts_differ"] = None not in facts.values() and len(set(facts.values())) > 1
    return item


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def _facts_flags(review: dict[str, Any]) -> int:
    return int(review["reviews"].get("facts", {}).get("flags", 0))


def summarize(items: list[dict[str, Any]], bundles: Iterable[str],
              calls: list[dict[str, Any]], *, unavailable: int = 0,
              stopped: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"items": len(items), "bundles": {}}
    for bundle in bundles:
        rows = [item[bundle] for item in items]
        errors: dict[str, int] = {}
        for row in rows:
            for key in (row.get("draft_error_type"), row["published"].get("error_type"),
                        row["full"].get("error_type")):
                if key:
                    errors[key] = errors.get(key, 0) + 1
        by_model: dict[str, dict[str, int]] = {}
        phases: dict[str, int] = {}
        bundle_calls = [call for call in calls if call["bundle"] == bundle]
        for call in bundle_calls:
            if "phase" in call:
                phases[call["phase"]] = phases.get(call["phase"], 0) + 1
            stats = by_model.setdefault(f"{call['role']}:{call['model']}", {
                "calls": 0, "errors": 0, "input_chars": 0, "output_chars": 0})
            stats["calls"] += 1
            stats["errors"] += 1 if "error_type" in call else 0
            stats["input_chars"] += call["input_chars"]
            stats["output_chars"] += call["output_chars"]
        result["bundles"][bundle] = {
            "full_double_approval": sum(1 for row in rows if row["full"]["double_approval"]),
            "published_double_approval": sum(
                1 for row in rows if row["published"]["double_approval"]),
            "drafts_ready": sum(1 for row in rows if row["draft_status"] == "ready"),
            "full_facts_flags": sum(_facts_flags(row["full"]) for row in rows),
            "published_facts_flags": sum(_facts_flags(row["published"]) for row in rows),
            "call_errors": sum(row["call_errors"] for row in rows),
            "error_types": errors,
            "error_phases": phases,
            "retries": sum(1 for call in bundle_calls if call.get("retry")),
            "call_error_share": (round(sum(1 for call in bundle_calls if "error_type" in call)
                                       / len(bundle_calls), 2) if bundle_calls else None),
            "median_seconds_per_item": _median([row["seconds"] for row in rows]),
            "median_input_chars_per_item": _median([row["input_chars"] for row in rows]),
            "median_output_chars_per_item": _median([row["output_chars"] for row in rows]),
            "by_role_and_model": by_model,
        }
    names = list(result["bundles"])
    result["pairs_published_reviewed"] = sum(
        1 for item in items if all(_reviewed(item[name]["published"]) for name in names))
    result["pairs_full_reviewed"] = sum(
        1 for item in items if all(_reviewed(item[name]["full"]) for name in names))
    result["facts_verdicts_differ"] = sum(1 for item in items if item["facts_verdicts_differ"])
    result["unavailable"] = unavailable
    result["stopped_for_quota"] = stopped
    result["valid"] = not stopped and all(
        (stats["call_error_share"] or 0) <= INVALID_ERROR_SHARE
        for stats in result["bundles"].values())
    return result


def _reviewed(review: dict[str, Any]) -> bool:
    """Both reviews ran to a verdict without a technical error."""
    return "error_type" not in review and bool(review["reviews"]) and all(
        entry.get("verdict") is not None for entry in review["reviews"].values())


def public_result(result: dict[str, Any]) -> dict[str, Any]:
    """The only object written to the uploaded artifact: no per-item data or ids."""
    return {key: result[key] for key in ("version", "generated_at", "models", "summary")}


def run_trial(database: Path, bundles: dict[str, dict[str, str]],
              factory: Callable[[str], Any], *, limit: int,
              budget: WaitBudget | None = None) -> dict[str, Any]:
    """Run every model bundle on the same published packages of a private snapshot copy.

    When a quota pause does not fit the budget, the item in progress is counted as
    technically unavailable and the remaining items are not started.
    """
    log = CallLog()
    budget = budget if budget is not None else WaitBudget()
    providers: dict[str, StructuredAIProvider] = {
        name: TimedProvider(RoleRoutedProvider(validate_models(models), factory), log, name,
                            budget)
        for name, models in bundles.items()}
    engine = create_engine(f"sqlite:///{database}")
    items: list[dict[str, Any]] = []
    unavailable = 0
    try:
        with Session(engine) as session:
            for post, package in published_packages(session, limit):
                item = trial_item(post, package, providers, log)
                if budget.stopped:
                    unavailable += 1
                    break
                items.append(item)
            session.rollback()
    finally:
        engine.dispose()
    return {"version": 3, "generated_at": datetime.now(UTC).isoformat(), "models": bundles,
            "summary": summarize(items, bundles, log.calls, unavailable=unavailable,
                                 stopped=budget.stopped),
            "items": items}


def markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    verdict = "действительна" if summary["valid"] else "недействительна"
    lines = ["## Проба моделей (#312)", "", f"Материалов в паре: {summary['items']}",
             f"Пар, где обе связки дошли до вердикта: опубликованный черновик "
             f"{summary['pairs_published_reviewed']}, новый черновик "
             f"{summary['pairs_full_reviewed']}",
             f"Технически недоступно: {summary['unavailable']}; остановлена из-за квоты: "
             f"{'да' if summary['stopped_for_quota'] else 'нет'}; проба {verdict}", "",
             "| Связка | Модели | Полная связка: оба ревью одобрили | Черновик готов | "
             "Флаги фактов в новом черновике | Ревью опубликованного: оба одобрили | "
             "Ошибки вызовов | Медиана, с | Медиана ввода, симв. | Медиана вывода, симв. |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, stats in summary["bundles"].items():
        models = ", ".join(f"{role}={model}" for role, model in result["models"][name].items())
        lines.append(
            f"| {name} | {models} | {stats['full_double_approval']} | "
            f"{stats['drafts_ready']} | {stats['full_facts_flags']} | "
            f"{stats['published_double_approval']} | {stats['call_errors']} | "
            f"{stats['median_seconds_per_item']} | {stats['median_input_chars_per_item']} | "
            f"{stats['median_output_chars_per_item']} |")
    lines += ["", "| Связка | Роль и модель | Вызовы | Ошибки | Ввод, симв. | Вывод, симв. |",
              "|---|---|---|---|---|---|"]
    for name, stats in summary["bundles"].items():
        for model, usage in stats["by_role_and_model"].items():
            lines.append(f"| {name} | {model} | {usage['calls']} | {usage['errors']} | "
                         f"{usage['input_chars']} | {usage['output_chars']} |")
    lines += ["", "| Связка | Повторы | Доля ошибок | Этапы ошибок |", "|---|---|---|---|"]
    for name, stats in summary["bundles"].items():
        phases = ", ".join(f"{phase}={count}" for phase, count in stats["error_phases"].items())
        lines.append(f"| {name} | {stats['retries']} | {stats['call_error_share']} | "
                     f"{phases or '—'} |")
    lines += ["", f"Расхождений вердикта фактов по опубликованным: "
              f"{summary['facts_verdicts_differ']}"]
    return "\n".join(lines) + "\n"


def write_outputs(result: dict[str, Any], output: Path, step_summary: str | None) -> None:
    """Write the uploaded artifact (public part only) and the job summary table."""
    output.write_text(json.dumps(public_result(result), ensure_ascii=False, indent=2),
                      encoding="utf-8")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write(markdown(result))


READY_ATTEMPTS = 20
READY_PAUSE_SECONDS = 30.0


def wait_ready(read: Callable[[], dict[str, Any]], validate: Callable[[dict[str, Any]], None],
               *, attempts: int = READY_ATTEMPTS, pause: float = READY_PAUSE_SECONDS,
               sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Read canonical state until it is ready; a running cycle holds it busy for minutes.

    The trial never claims or resets the marker: it only waits and reads again.
    """
    for attempt in range(attempts):
        state = read()
        if state.get("status") != "busy":
            validate(state)
            return state
        if attempt + 1 < attempts:
            sleep(pause)
    raise TimeoutError("canonical state stayed busy")


def main() -> None:
    from relocant_media.ai.gemini_process import IsolatedGeminiProvider
    from relocant_media.ops.actions_runtime import build_storage, restored, validate_ready

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=MAX_ITEMS)
    args = parser.parse_args()
    stage = "configuration"
    try:
        current = role_models(os.environ)
        bundles = {"current": current,
                   "candidate": {"default": current["default"],
                                 "draft": os.environ.get("TRIAL_MODEL_DRAFT", "").strip()
                                 or current["draft"],
                                 "review": os.environ.get("TRIAL_MODEL_REVIEW", "").strip()
                                 or current["review"]}}
        api_key = os.environ["GEMINI_API_KEY"]
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            stage = "storage"
            store, blobs, _ = build_storage(client, os.environ)
            stage = "state"
            state = wait_ready(lambda: store.read()[0], validate_ready)
            stage = "snapshot"
            args.work.mkdir(parents=True, exist_ok=False, mode=0o700)
            database = restored(blobs, state["snapshot"], args.work)
        stage = "trial"
        result = run_trial(database, bundles,
                           lambda model: IsolatedGeminiProvider(api_key, model=model),
                           limit=args.limit)
        write_outputs(result, args.output, os.environ.get("GITHUB_STEP_SUMMARY"))
        print(json.dumps({"model_trial": result["summary"]}, ensure_ascii=False))
    except Exception as error:
        # Exception text can contain URLs, credentials or response bodies.
        print(json.dumps({"model_trial_error": type(error).__name__, "stage": stage}),
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
