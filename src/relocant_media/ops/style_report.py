"""Отчёт о соблюдении матрицы подачи на опубликованных постах (#313).

Только чтение приватной копии снимка: состояние не занимается и не меняется, Telegram
и модели не вызываются. В отчёт попадают только агрегаты по категориям: без текстов,
заголовков и номеров постов. Флаги — подсказки редактору, не блок публикации.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.channel_style import CHANNEL_STYLE_VERSION, word_range
from relocant_media.domain.style_lint import LONG_PARAGRAPH_WORDS, lint
from relocant_media.storage.models import EditorialRun, Post

MAX_DAYS = 30


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def build_report(session: Session, *, now: datetime, days: int) -> dict[str, Any]:
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"days must be between 1 and {MAX_DAYS}")
    since = now - timedelta(days=days)
    rows = session.execute(
        select(Post, EditorialRun.outcome).join(EditorialRun, EditorialRun.post_id == Post.id)
        .where(EditorialRun.delivery_status == "sent", Post.telegram_message_id > 0,
               Post.published_at.is_not(None))).all()
    groups: dict[str, list[Any]] = {}
    reader_posts = 0
    for row in rows:
        post: Post = row[0]
        outcome = row[1]
        if post.published_at is None or _aware(post.published_at) < since:
            continue
        # Reader material follows the author's text, not the editorial matrix.
        if isinstance(outcome, dict) and "_reader_submission_id" in outcome:
            reader_posts += 1
            continue
        groups.setdefault(post.category, []).append(
            lint(post.body, word_range(post.category)))
    categories: dict[str, Any] = {}
    for category, flags in sorted(groups.items()):
        terms: Counter[str] = Counter()
        for item in flags:
            terms.update(item.bureaucratese)
        low, high = word_range(category)
        categories[category] = {
            "posts": len(flags), "word_range": [low, high],
            "median_words": statistics.median(item.words for item in flags),
            "over_range": sum(1 for item in flags if item.over_range),
            "under_range": sum(1 for item in flags if item.under_range),
            "with_long_paragraph": sum(1 for item in flags if item.long_paragraphs),
            "with_bureaucratese": sum(1 for item in flags if item.bureaucratese),
            "bureaucratese_terms": dict(sorted(terms.items())),
        }
    return {"version": 1, "generated_at": now.isoformat(), "days": days,
            "matrix_version": CHANNEL_STYLE_VERSION,
            "long_paragraph_words": LONG_PARAGRAPH_WORDS,
            "posts": sum(len(flags) for flags in groups.values()),
            "reader_posts_excluded": reader_posts, "categories": categories}


def markdown(report: dict[str, Any]) -> str:
    lines = [f"## Матрица подачи за {report['days']} дн. ({report['matrix_version']})", "",
             f"Постов: {report['posts']}; материалы читателей не считаются "
             f"({report['reader_posts_excluded']}). Флаги — подсказки, не блок.", "",
             "| Категория | Постов | Диапазон | Медиана слов | Длиннее | Короче | "
             f"Абзац >{report['long_paragraph_words']} | Канцелярит |",
             "|---|---|---|---|---|---|---|---|"]
    for category, stats in report["categories"].items():
        low, high = stats["word_range"]
        lines.append(
            f"| {category} | {stats['posts']} | {low}–{high} | {stats['median_words']} | "
            f"{stats['over_range']} | {stats['under_range']} | "
            f"{stats['with_long_paragraph']} | {stats['with_bureaucratese']} |")
    terms: Counter[str] = Counter()
    for stats in report["categories"].values():
        terms.update(stats["bureaucratese_terms"])
    lines += ["", "Канцелярит: " + (", ".join(f"«{term}» {count}" for term, count
                                            in terms.most_common()) or "не найден")]
    return "\n".join(lines) + "\n"


def main() -> None:
    from relocant_media.ops.actions_runtime import build_storage, restored, validate_ready
    from relocant_media.ops.model_trial import wait_ready

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--days", type=int, default=7)
    args = parser.parse_args()
    stage = "storage"
    try:
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            store, blobs, _ = build_storage(client, os.environ)
            stage = "state"
            state = wait_ready(lambda: store.read()[0], validate_ready)
            stage = "snapshot"
            args.work.mkdir(parents=True, exist_ok=False, mode=0o700)
            database = restored(blobs, state["snapshot"], args.work)
        stage = "report"
        engine = create_engine(f"sqlite:///{database}")
        try:
            with Session(engine) as session:
                report = build_report(session, now=datetime.now(UTC), days=args.days)
        finally:
            engine.dispose()
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8") as handle:
                handle.write(markdown(report))
        print(json.dumps({"style_report": report}, ensure_ascii=False))
    except Exception as error:
        # Exception text can contain URLs, credentials or response bodies.
        print(json.dumps({"style_report_error": type(error).__name__, "stage": stage}),
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
