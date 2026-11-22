"""Read-only model trial (#312): compare candidate draft/review models on published posts.

The trial restores the verified canonical snapshot into a private working copy, takes the
latest published posts with stored reviewed packages and asks candidate models to write a new
draft and to review the published draft again. Nothing is written back to canonical state,
no Telegram API is used, and reader submissions are excluded because free-tier model input
may be used by the provider.
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
from relocant_media.ai.model_roles import RoleRoutedProvider, role_for
from relocant_media.ai.package import EditorialPackage
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.storage.models import EditorialRun, Post

MAX_ITEMS = 15
_MODEL = r"[a-z0-9][a-z0-9.\-]{1,63}"


@dataclass
class CallLog:
    calls: list[dict[str, Any]] = field(default_factory=list)


class TimedProvider:
    """Record task, model, duration and success of each call; never the prompt or answer."""

    def __init__(self, inner: RoleRoutedProvider, log: CallLog) -> None:
        self._inner, self._log = inner, log

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        started = time.monotonic()
        entry: dict[str, Any] = {"task": task, "role": role_for(task),
                                 "model": self._inner.model_for(task)}
        try:
            result = self._inner.generate_json(
                task=task, system_instruction=system_instruction, prompt=prompt,
                response_schema=response_schema)
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


def published_packages(session: Session, limit: int) -> list[tuple[Post, EditorialPackage]]:
    """Latest sent posts with a stored reviewed package, excluding reader submissions."""
    if not 1 <= limit <= MAX_ITEMS:
        raise ValueError(f"limit must be between 1 and {MAX_ITEMS}")
    rows = session.execute(
        select(EditorialRun, Post).join(Post, EditorialRun.post_id == Post.id)
        .where(EditorialRun.delivery_status == "sent", Post.telegram_message_id.is_not(None))
        .order_by(Post.published_at.desc()).limit(limit * 4)).all()
    result: list[tuple[Post, EditorialPackage]] = []
    for run, post in rows:
        outcome = run.outcome or {}
        if "_reader_submission_id" in outcome or "_package" not in outcome:
            continue
        try:
            package = EditorialPackage.model_validate_json(json.dumps(outcome["_package"]))
        except ValueError:
            continue
        if package.version != 2 or package.event is None:
            continue
        result.append((post, package))
        if len(result) == limit:
            break
    return result


def _review_summary(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: entry.get(key) for key in ("role", "verdict", "computed_score", "flags",
                                             "reason")} for entry in entries]


def _stored_reviews(package: EditorialPackage) -> list[dict[str, Any]]:
    return [{"role": review.role, "verdict": review.verdict, "score": review.score,
             "flags": list(review.flags)} for review in package.reviews]


def trial_item(post: Post, package: EditorialPackage, provider: StructuredAIProvider,
               log: CallLog) -> dict[str, Any]:
    assert package.event is not None and package.outcome.draft is not None
    when = post.published_at or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    first = len(log.calls)
    item: dict[str, Any] = {
        "raw_item_id": package.raw_item_id, "source_id": package.source_id,
        "published_at": when.isoformat(), "category": package.outcome.ranking.category,
        "published": {"title": package.outcome.draft.title, "body": package.outcome.draft.body,
                      "reviews": _stored_reviews(package)},
    }
    documents = tuple(package.event.documents)
    try:
        outcome = EditorialPipeline(provider, city_profile=load_city_profile(package.city)).run(
            city=package.city, documents=documents, now=when)
        item["candidate_draft"] = {
            "status": outcome.status.value, "reason": outcome.reason,
            **({"title": outcome.draft.title, "body": outcome.draft.body}
               if outcome.draft is not None else {})}
    except Exception as error:
        item["candidate_draft"] = {"error_type": type(error).__name__}
    placeholders = tuple(review.model_copy(update={
        "verdict": "hold", "score": 0, "flags": (), "live_source_checked": False,
        "facts_check": None}) for review in package.reviews)
    audit: list[dict[str, Any]] = []
    try:
        reviewed_package(package.model_copy(update={"reviews": placeholders}), provider,
                         lambda: documents, now=when, audit=audit.append)
        item["candidate_reviews_of_published"] = _review_summary(audit)
    except Exception as error:
        item["candidate_reviews_of_published"] = {"error_type": type(error).__name__,
                                                  "partial": _review_summary(audit)}
    item["calls"] = log.calls[first:]
    return item


def summarize(items: list[dict[str, Any]], calls: list[dict[str, Any]]) -> dict[str, Any]:
    by_model: dict[str, dict[str, Any]] = {}
    for call in calls:
        stats = by_model.setdefault(f"{call['role']}:{call['model']}",
                                    {"calls": 0, "errors": 0, "seconds": []})
        stats["calls"] += 1
        stats["errors"] += 1 if "error_type" in call else 0
        stats["seconds"].append(call["seconds"])
    for stats in by_model.values():
        seconds = sorted(stats.pop("seconds"))
        stats["median_seconds"] = seconds[len(seconds) // 2] if seconds else None
        stats["max_seconds"] = seconds[-1] if seconds else None
    disagreements = sum(
        1 for item in items if isinstance(item.get("candidate_reviews_of_published"), list)
        and any(review.get("verdict") != "approve"
                for review in item["candidate_reviews_of_published"]))
    return {"items": len(items), "drafts_ready": sum(
        1 for item in items if item["candidate_draft"].get("status") == "ready"),
        "published_posts_not_approved_by_candidate_reviews": disagreements,
        "by_role_and_model": by_model}


def run_trial(database: Path, models: dict[str, str], factory: Callable[[str], Any], *,
              limit: int) -> dict[str, Any]:
    log = CallLog()
    provider = TimedProvider(RoleRoutedProvider(validate_models(models), factory), log)
    engine = create_engine(f"sqlite:///{database}")
    try:
        with Session(engine) as session:
            selected = published_packages(session, limit)
            items = [trial_item(post, package, provider, log) for post, package in selected]
            session.rollback()
    finally:
        engine.dispose()
    return {"version": 1, "generated_at": datetime.now(UTC).isoformat(), "models": models,
            "summary": summarize(items, log.calls), "items": items}


def markdown(result: dict[str, Any]) -> str:
    summary = result["summary"]
    lines = ["## Проба моделей (#312)", "",
             f"Модели: {json.dumps(result['models'], ensure_ascii=False)}", "",
             f"Материалов: {summary['items']}, готовых черновиков: {summary['drafts_ready']}, "
             "опубликованных постов без одобрения новых ревью: "
             f"{summary['published_posts_not_approved_by_candidate_reviews']}", "",
             "| Роль и модель | Вызовы | Ошибки | Медиана, с | Максимум, с |",
             "|---|---|---|---|---|"]
    for name, stats in summary["by_role_and_model"].items():
        lines.append(f"| {name} | {stats['calls']} | {stats['errors']} | "
                     f"{stats['median_seconds']} | {stats['max_seconds']} |")
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
        default = os.environ["GEMINI_MODEL"].strip()
        models = {"default": default,
                  "draft": os.environ.get("TRIAL_MODEL_DRAFT", "").strip() or default,
                  "review": os.environ.get("TRIAL_MODEL_REVIEW", "").strip() or default}
        api_key = os.environ["GEMINI_API_KEY"]
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            store, blobs, _ = build_storage(client, os.environ)
            state, _ = store.read()
            validate_ready(state)
            args.work.mkdir(parents=True, exist_ok=False, mode=0o700)
            database = restored(blobs, state["snapshot"], args.work)
        result = run_trial(database, models,
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
