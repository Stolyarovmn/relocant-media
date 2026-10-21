"""Operational newsroom status from run artifacts or canonical database state."""

import argparse
import json
from pathlib import Path
from typing import Any

from relocant_media.config import settings
from relocant_media.services.newsroom_control import build_newsroom_inbox
from relocant_media.services.shadow_duplicates import build_shadow_duplicate_report
from relocant_media.services.source_revision_control import build_source_revision_control
from relocant_media.storage.db import SessionFactory


def summarize(output: Path) -> dict[str, Any]:
    def read(name: str) -> dict[str, Any]:
        path = output / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    report, ingest, triage, topics = (
        read(name)
        for name in ("server-report.json", "ingest.json", "triage.json", "channel-topics.json")
    )
    decisions = [read(path.name) for path in sorted(output.glob("editorial-*.json"))]
    calls = output / "ai-calls.jsonl"
    ai = (
        [
            json.loads(line)
            for line in calls.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if calls.is_file()
        else []
    )
    return {
        "run_directory": output.name,
        "status": report.get("status", "incomplete"),
        "discarded_without_ai": ingest.get("discarded_before_article_fetch", 0)
        + ingest.get("discarded_before_persistence", 0)
        + triage.get("discarded", 0),
        "channel_topics": len(topics.get("topics", [])),
        "channel_topics_deferred": sum(
            topic["status"] == "deferred_missing_primary_source"
            for topic in topics.get("topics", [])
        ),
        "source_failures": ingest.get("failures", 0),
        "venue_article_failures": ingest.get("venue_article_failures", 0),
        "priority_submissions": ingest.get("priority_submissions", 0),
        "primary_searches": ingest.get("primary_searches", 0),
        "private_intake": report.get("private_intake", {}),
        "private_feedback": report.get("private_feedback", {}),
        "published_source_watch": read("published-source-watch.json"),
        "channel_failures": ingest.get("channel_failures", 0),
        "ai_calls": len(ai),
        "ai_failures": [call for call in ai if call["status"] == "failed"],
        "editorial_decisions": decisions,
        "sent_message_ids": report.get("sent_message_ids", []),
        "remaining_count": report.get("remaining_count"),
        "processing_errors": report.get("processing_errors", []),
        "channel_blocked": report.get("channel_blocked", []),
        "pin_maintenance": report.get("pin_maintenance", {}),
        "audience_observation": report.get("audience_observation", {}),
        "cycle_audit_status": report.get("cycle_audit_status"),
        "note": (
            "Отложено по правилам или без первоисточника — "
            "не означает активную проверку человеком."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--runs", type=Path)
    source.add_argument(
        "--database",
        action="store_true",
        help="build the read-only event inbox from the canonical editorial database",
    )
    parser.add_argument("--city", default=settings.city_id)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument(
        "--shadow-duplicates",
        action="store_true",
        help="include non-authoritative possible-duplicate signals; never changes decisions",
    )
    args = parser.parse_args()

    if args.database:
        with SessionFactory() as session:
            payload = build_newsroom_inbox(session, city=args.city, limit=args.limit)
            payload["source_revision_control"] = build_source_revision_control(
                session,
                city=args.city,
                limit=args.limit,
            )
            if args.shadow_duplicates:
                payload["shadow_duplicates"] = build_shadow_duplicate_report(
                    session,
                    city=args.city,
                    limit=args.limit,
                )
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return

    if args.shadow_duplicates:
        parser.error("--shadow-duplicates requires --database")
    assert args.runs is not None
    outputs = sorted(path for path in args.runs.iterdir() if path.is_dir())
    if not outputs:
        raise ValueError("no runs found")
    print(json.dumps(summarize(outputs[-1]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
