"""Collect an explicit source allowlist in staging, without AI or Telegram settings."""

import argparse
import asyncio
import json
import os
from dataclasses import asdict
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ingestion.runner import ingest_city


async def collect(city: str, sources: Path, source_ids: tuple[str, ...]) -> dict[str, int]:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging is required")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("explicit DATABASE_URL is required")
    engine = create_engine(database_url)
    try:
        async with httpx.AsyncClient(
            timeout=30, trust_env=False,
            headers={"User-Agent": "relocant-media/0.1 (+source-monitoring)"},
        ) as client:
            with Session(engine) as session:
                result = await ingest_city(
                    city, sources, session, client, source_ids=source_ids,
                )
        return asdict(result)
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect explicit staging sources without AI")
    parser.add_argument("--city", default="belgrade")
    parser.add_argument("--sources", type=Path, default=Path("cities/belgrade/sources"))
    parser.add_argument("--source-id", action="append", required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(collect(args.city, args.sources, tuple(args.source_id)))
    except Exception:
        parser.exit(1, "Сбор не выполнен: проверьте staging, источники и доступ к БД.\n")
    print(json.dumps(result, sort_keys=True))
    if result["failures"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
