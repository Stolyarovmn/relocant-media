"""Prepare and finalize production media review artifacts without Telegram I/O."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.services.media_release_finalizer import (
    finalize_media_release,
    prepare_media_release_review_bundle,
)


def _require_production() -> None:
    if os.environ.get("APP_ENV", "").lower() != "production":
        raise ValueError("APP_ENV=production required")


async def prepare(
    *,
    raw_item_id: int,
    release_id: str,
    assets_manifest: Path,
    assets: Path,
    output: Path,
) -> dict[str, object]:
    _require_production()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("explicit DATABASE_URL required")
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            return await prepare_media_release_review_bundle(
                session,
                raw_item_id=raw_item_id,
                release_id=release_id,
                assets_manifest=assets_manifest,
                assets=assets,
                output=output,
            )
    finally:
        engine.dispose()


def finalize(*, bundle: Path, reviews: Path) -> dict[str, object]:
    _require_production()
    return finalize_media_release(bundle, reviews_path=reviews)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Freeze exact media review context and seal two matching approvals",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--raw-item-id", required=True, type=int)
    prepare_parser.add_argument("--release-id", required=True)
    prepare_parser.add_argument("--assets-manifest", required=True, type=Path)
    prepare_parser.add_argument("--assets", required=True, type=Path)
    prepare_parser.add_argument("--output", required=True, type=Path)

    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--bundle", required=True, type=Path)
    finalize_parser.add_argument("--reviews", required=True, type=Path)

    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = asyncio.run(
                prepare(
                    raw_item_id=args.raw_item_id,
                    release_id=args.release_id,
                    assets_manifest=args.assets_manifest,
                    assets=args.assets,
                    output=args.output,
                )
            )
        else:
            result = finalize(bundle=args.bundle, reviews=args.reviews)
    except Exception as error:
        parser.exit(1, f"Media release blocked: {error}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
