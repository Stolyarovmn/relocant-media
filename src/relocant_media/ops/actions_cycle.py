"""Последовательный цикл SQLite без отправки сообщений."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from google import genai
from google.genai import types
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.gemini import GeminiStructuredProvider
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.config import settings
from relocant_media.ops.collect_cli import collect
from relocant_media.services.editorial import EditorialService
from relocant_media.storage.models import Base, EditorialRun, RawItem, Source


def run(*, analyze: bool, limit: int = 5) -> dict[str, object]:
    if os.environ.get("APP_ENV") != "staging":
        raise ValueError("Требуется APP_ENV=staging")
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("sqlite:///") or url.endswith(":memory:"):
        raise ValueError("Требуется файловая SQLite")
    if not 1 <= limit <= 5:
        raise ValueError("Лимит должен быть от 1 до 5")
    engine = create_engine(url)
    try:
        Base.metadata.create_all(engine)
        result: dict[str, object] = {"collection": asyncio.run(collect(
            "belgrade", Path("cities/belgrade/sources"), ("city_beograd_news",),
        )), "publication": "Не публиковалось"}
        if analyze:
            pipeline = EditorialPipeline(GeminiStructuredProvider(
                settings.gemini_api_key, model=settings.gemini_model,
                client=genai.Client(api_key=settings.gemini_api_key, http_options=types.HttpOptions(
                    timeout=60_000, retry_options=types.HttpRetryOptions(attempts=1),
                )),
            ))
            with Session(engine) as session:
                ids = list(session.scalars(select(RawItem.id).join(Source).where(
                    Source.city == "belgrade", Source.enabled.is_(True),
                    RawItem.published_at >= datetime.now(UTC) - timedelta(days=7),
                    ~select(EditorialRun.raw_item_id).where(
                        EditorialRun.raw_item_id == RawItem.id,
                    ).exists(),
                ).order_by(RawItem.published_at, RawItem.id).limit(limit)))
                service = EditorialService(session, pipeline)
                for item_id in ids:
                    service.prepare(item_id)
                result["prepared"] = len(ids)
        return result
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyze", action="store_true")
    args = parser.parse_args()
    try:
        result = run(analyze=args.analyze)
    except Exception as error:
        # Не выводить ответ SDK: он может содержать запрос или служебные данные.
        Path("reports").mkdir(exist_ok=True)
        Path("reports/summary.json").write_text(json.dumps({
            "error_type": type(error).__name__, "publication": "Не публиковалось",
        }, ensure_ascii=False), encoding="utf-8")
        parser.exit(1, "Обработка не выполнена; тип ошибки сохранён в отчёте.\n")
    Path("reports").mkdir(exist_ok=True)
    Path("reports/summary.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    collection = result["collection"]
    if isinstance(collection, dict) and collection.get("failures"):
        raise SystemExit("Обнаружены ошибки сбора; состояние сохранено")


if __name__ == "__main__":
    main()
