"""Materialize explicitly reviewed media assessments into exact local assets."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.reviewed_media_materialization import (
    materialize_reviewed_media_assets,
)
from relocant_media.storage.sqlite_schema import verify_sqlite_schema


def _sqlite_url() -> URL:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise ValueError("explicit DATABASE_URL required")
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.query:
        raise ValueError("existing local SQLite required")
    database = Path(url.database)
    if not database.is_file():
        raise ValueError("existing local SQLite required")
    return url


def _load_assessments(path: Path) -> tuple[CoverAssessment, ...]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("assessment file must be a regular file")
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError("assessment file must contain a list")
    return tuple(CoverAssessment.model_validate(value) for value in values)


def run(
    *,
    raw_item_id: int,
    assessments: Path,
    event_year: int,
    output: Path,
    allow_album: bool = False,
) -> dict[str, object]:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging required")
    url = _sqlite_url()
    reviewed = _load_assessments(assessments)
    engine = create_engine(url)
    try:
        verify_sqlite_schema(engine)
        with Session(engine) as session, httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(20.0, connect=10.0),
        ) as client:
            return materialize_reviewed_media_assets(
                session,
                raw_item_id=raw_item_id,
                assessments=reviewed,
                event_year=event_year,
                output=output,
                client=client,
                allow_album=allow_album,
            )
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Download only explicitly reviewed media candidates into a provenance-bound bundle"
        ),
    )
    parser.add_argument("--raw-item-id", required=True, type=int)
    parser.add_argument("--assessments", required=True, type=Path)
    parser.add_argument("--event-year", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--allow-album", action="store_true")
    try:
        result = run(**vars(parser.parse_args()))
    except Exception as error:
        parser.exit(1, f"Media asset materialization blocked: {error}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
