"""Audit registered sources without database, model calls or Telegram access."""
import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from relocant_media.collectors.factory import build_collector
from relocant_media.domain.freshness import freshness_decision
from relocant_media.sources.registry import load_source_registry


async def audit_sources(directory: Path, *, online: bool = False,
                        source_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    sources = load_source_registry(directory)
    if source_ids:
        if set(source_ids) - {s.id for s in sources}:
            raise ValueError("unknown source id")
        sources = tuple(s for s in sources if s.id in source_ids)
    rows = []
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        for source in sources:
            row: dict[str, Any] = {"id": source.id, "name": source.name,
                "url": str(source.url), "type": source.source_type,
                "strategy": source.extraction.strategy, "auto_publish": source.policy.auto_publish,
                "status": "configured" if source.extraction.status == "active" else "planned"}
            if row["status"] == "configured" and online:
                try:
                    items = await build_collector(source, client).collect()
                    fresh = [i for i in items if freshness_decision(
                        i.title, i.body, i.published_at, datetime.now(UTC)).action == "keep"]
                    row.update(status="ok" if fresh else "empty", fetched_items=len(items),
                               fresh_items=len(fresh), examples=[
                                   {"title": i.title, "url": i.url,
                                    "published_at": i.published_at.isoformat()
                                    if i.published_at else None} for i in fresh[:2]])
                except Exception as error:
                    row.update(status="error", error_type=type(error).__name__)
            rows.append(row)
    return {"checked_at": datetime.now(UTC).isoformat(), "online": online,
            "ai_calls": 0, "sources": rows,
            "active_count": sum(r["status"] != "planned" for r in rows),
            "working_count": sum(r["status"] in {"ok", "empty"} for r in rows),
            "error_count": sum(r["status"] == "error" for r in rows)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("cities/belgrade/sources"))
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--source-id", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = asyncio.run(audit_sources(args.sources, online=args.online,
                                      source_ids=tuple(args.source_id)))
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
