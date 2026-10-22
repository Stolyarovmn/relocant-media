"""Install a fully reviewed media bundle into the production delivery inbox."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.services.production_media_install import install_production_media


async def run(*, release: Path, assets: Path, root: Path | None = None) -> dict[str, object]:
    if os.environ.get("APP_ENV", "").lower() != "production":
        raise ValueError("APP_ENV=production required")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("explicit DATABASE_URL required")
    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            return await install_production_media(
                session,
                release_path=release,
                assets=assets,
                root=root,
            )
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Atomically install an already reviewed production media release",
    )
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--assets", required=True, type=Path)
    parser.add_argument("--root", type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(run(**vars(args)))
    except Exception as error:
        parser.exit(1, f"Production media install blocked: {error}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
