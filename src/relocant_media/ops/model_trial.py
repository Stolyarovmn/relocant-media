"""Read-only model trial (#312): compare candidate draft/review models on published posts.

The trial restores the verified canonical snapshot into a private working copy, takes the
latest published posts with stored reviewed packages and runs the current and the candidate
model bundles on the same items: a new draft and a fresh review of the published draft.
Nothing is written back to canonical state, no Telegram API is used, and every package linked
to a reader submission is excluded because free-tier model input may be used by the provider.
The output is an anonymized summary: counts, error types, time and sizes, no texts or reasons.
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

from relocant_media.ai.independent_reviews import reviewed_package
from relocant_media.ai.model_roles import RoleRoutedProvider, role_for, role_models
from relocant_media.ai.package import EditorialPackage
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.storage.models import EditorialRun, Post, Submission

MAX_ITEMS = 15
_MODEL = r"[a-z0-9][a-z0-9.\-]{1,63}"


@dataclass
class CallLog:
    calls: list[dict[str, Any]] = field(default_factory=list)


class TimedProvider:
    """Record bundle, task, model, duration, sizes and success of each call.

    Prompt and answer text are never kept: only their lengths, as a cost proxy.
    """

    def __init__(self, inner: RoleRoutedProvider, log: CallLog, bundle: str) -> None:
        self._inner, self._log, self._bundle = inner, log, bundle

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        started = time.monotonic()
        entry: dict[str, Any] = {"bundle": self._bundle, "task": task, "role": role_for(task),
                                 "model": self._inner.model_for(task),
                                 "input_chars": len(system_instruction) + len(prompt),
                                 "output_chars": 0}
        try:
            result = self._inner.generate_json(
                task=task, system_instruction=system_instruction, prompt=prompt,
                response_schema=response_schema)
            entry["output_chars"] = len(result)
        except Exception as error:
            entry["error_type"] = type(error).__name__
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


def trial_bundle(package: EditorialPackage, when: datetime, provider: StructuredAIProvider,
                 log: CallLog) -> dict[str, Any]:
    """One model bundle on one published package: draft status and both review verdicts."""
    first = len(log.calls)
    documents = tuple(package.event.documents) if package.event is not None else ()
    result: dict[str, Any] = {}
    try:
        outcome = EditorialPipeline(provider, city_profile=load_city_profile(package.city)).run(
            city=package.city, documents=documents, now=when)
        result["draft_status"] = outcome.status.value
    except Exception as error:
        result["draft_status"] = "error"
        result["draft_error_type"] = type(error).__name__
    placeholders = tuple(review.model_copy(update={
        "verdict": "hold", "score": 0, "flags": (), "live_source_checked": False,
        "facts_check": None}) for review in package.reviews)
    audit: list[dict[str, Any]] = []
    try:
        reviewed_package(package.model_copy(update={"reviews": placeholders}), provider,
                         lambda: documents, now=when, audit=audit.append)
    except Exception as error:
        result["review_error_type"] = type(error).__name__
    result["reviews"] = _verdicts(audit)
    result["double_approval"] = ("review_error_type" not in result and bool(audit) and all(
        entry.get("verdict") == "approve" for entry in audit))
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
    facts = {bundle: item[bundle]["reviews"].get("facts", {}).get("verdict")
             for bundle in providers}
    item["facts_verdicts_differ"] = len(set(facts.values())) > 1
    return item


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2, 2)


def summarize(items: list[dict[str, Any]], bundles: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"items": len(items), "bundles": {}}
    for bundle in bundles:
        rows = [item[bundle] for item in items]
        errors: dict[str, int] = {}
        for row in rows:
            for key in ("draft_error_type", "review_error_type"):
                if key in row:
                    errors[row[key]] = errors.get(row[key], 0) + 1
        result["bundles"][bundle] = {
            "double_approval": sum(1 for row in rows if row["double_approval"]),
            "drafts_ready": sum(1 for row in rows if row["draft_status"] == "ready"),
            "facts_flags": sum(row["reviews"].get("facts", {}).get("flags", 0) for row in rows),
            "call_errors": sum(row["call_errors"] for row in rows),
            "error_types": errors,
            "median_seconds_per_item": _median([row["seconds"] for row in rows]),
            "median_input_chars_per_item": _median([row["input_chars"] for row in rows]),
            "median_output_chars_per_item": _median([row["output_chars"] for row in rows]),
        }
    result["facts_verdicts_differ"] = [item["raw_item_id"] for item in items
                                       if item["facts_verdicts_differ"]]
    return result


def run_trial(database: Path, bundles: dict[str, dict[str, str]],
              factory: Callable[[str], Any], *, limit: int) -> dict[str, Any]:
    """Run every model bundle on the same published packages of a private snapshot copy."""
    log = CallLog()
    providers: dict[str, StructuredAIProvider] = {
        name: TimedProvider(RoleRoutedProvider(validate_models(models), factory), log, name)
        for name, models in bundles.items()}
    engine = create_engine(f"sqlite:///{database}")
    try:
        with Session(engine) as session:
            selected = published_packages(session, limit)
            items = [trial_item(post, package, providers, log) for post, package in selected]
            session.rollback()
    finally:
        engine.dispose()
    return {"version": 2, "generated_at": datetime.now(UTC).isoformat(), "models": bundles,
            "summary": summarize(items, bundles), "items": items}


def markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = ["## Проба моделей (#312)", "", f"Материалов в паре: {summary['items']}", "",
             "| Связка | Модели | Оба ревью одобрили | Черновик готов | Флаги фактов | "
             "Ошибки вызовов | Медиана, с | Медиана ввода, симв. | Медиана вывода, симв. |",
             "|---|---|---|---|---|---|---|---|---|"]
    for name, stats in summary["bundles"].items():
        models = ", ".join(f"{role}={model}" for role, model in result["models"][name].items())
        lines.append(
            f"| {name} | {models} | {stats['double_approval']} | {stats['drafts_ready']} | "
            f"{stats['facts_flags']} | {stats['call_errors']} | "
            f"{stats['median_seconds_per_item']} | {stats['median_input_chars_per_item']} | "
            f"{stats['median_output_chars_per_item']} |")
    differ = summary["facts_verdicts_differ"]
    lines += ["", "Расхождения вердикта фактов (raw_item_id): "
              + (", ".join(str(value) for value in differ) if differ else "нет")]
    return "\n".join(lines) + "\n"


def main() -> None:
    from relocant_media.ai.gemini_process import IsolatedGeminiProvider
    from relocant_media.ops.actions_runtime import build_storage, restored, validate_ready

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=MAX_ITEMS)
    args = parser.parse_args()
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
            store, blobs, _ = build_storage(client, os.environ)
            state, _ = store.read()
            validate_ready(state)
            args.work.mkdir(parents=True, exist_ok=False, mode=0o700)
            database = restored(blobs, state["snapshot"], args.work)
        result = run_trial(database, bundles,
                           lambda model: IsolatedGeminiProvider(api_key, model=model),
                           limit=args.limit)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with Path(summary_path).open("a", encoding="utf-8") as handle:
                handle.write(markdown(result))
        print(json.dumps({"model_trial": result["summary"]}, ensure_ascii=False))
    except Exception as error:
        # Exception text can contain URLs, credentials or response bodies.
        print(json.dumps({"model_trial_error": type(error).__name__}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
