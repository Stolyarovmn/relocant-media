"""Prepare downloaded reviewed media and seal it after two explicit media reviews."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.media_release_preparation import (
    prepare_media_release_bundle,
    seal_media_release_bundle,
)
from relocant_media.storage.sqlite_schema import verify_sqlite_schema


def _require_staging() -> None:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging required")


def _sqlite_url():
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


async def run_prepare(
    *,
    raw_item_id: int,
    assessments: Path,
    event_year: int,
    output: Path,
    allow_album: bool = False,
) -> dict[str, object]:
    _require_staging()
    url = _sqlite_url()
    reviewed = _load_assessments(assessments)
    engine = create_engine(url)
    try:
        verify_sqlite_schema(engine)
        with Session(engine) as session:
            return await prepare_media_release_bundle(
                session,
                raw_item_id=raw_item_id,
                assessments=reviewed,
                event_year=event_year,
                output=output,
                allow_album=allow_album,
            )
    finally:
        engine.dispose()


def run_seal(*, bundle: Path, reviews: Path) -> dict[str, object]:
    _require_staging()
    return seal_media_release_bundle(bundle=bundle, reviews_path=reviews)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare or seal provenance-bound production media without Telegram I/O",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare = commands.add_parser("prepare")
    prepare.add_argument("--raw-item-id", required=True, type=int)
    prepare.add_argument("--assessments", required=True, type=Path)
    prepare.add_argument("--event-year", required=True, type=int)
    prepare.add_argument("--output", required=True, type=Path)
    prepare.add_argument("--allow-album", action="store_true")

    seal = commands.add_parser("seal")
    seal.add_argument("--bundle", required=True, type=Path)
    seal.add_argument("--reviews", required=True, type=Path)

    args = vars(parser.parse_args())
    command = args.pop("command")
    try:
        result = (
            asyncio.run(run_prepare(**args))
            if command == "prepare"
            else run_seal(**args)
        )
    except Exception as error:
        parser.exit(1, f"Media release operation blocked: {error}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
