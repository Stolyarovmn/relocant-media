import argparse
import asyncio
import json
from dataclasses import asdict
from pathlib import Path

import httpx

from relocant_media.ingestion.runner import ingest_city
from relocant_media.storage.db import SessionFactory


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Relocant Media ingestion cycle")
    parser.add_argument("--city", default="belgrade")
    parser.add_argument("--sources", type=Path, default=Path("cities/belgrade/sources"))
    return parser


async def _run(city: str, sources: Path) -> dict[str, int]:
    headers = {"User-Agent": "relocant-media/0.1 (+source-monitoring)"}
    timeout = httpx.Timeout(30.0)
    async with httpx.AsyncClient(headers=headers, timeout=timeout) as client:
        with SessionFactory() as session:
            summary = await ingest_city(city, sources, session, client)
    return asdict(summary)


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(_run(args.city, args.sources))
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
