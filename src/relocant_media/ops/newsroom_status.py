"""Compact operational report without article bodies, credentials or AI calls."""
import argparse
import json
from pathlib import Path
from typing import Any


def summarize(output: Path) -> dict[str, Any]:
    def read(name: str) -> dict[str, Any]:
        path = output / name
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    report, ingest, triage, topics = (read(name) for name in
        ("server-report.json", "ingest.json", "triage.json", "channel-topics.json"))
    decisions = [read(path.name) for path in sorted(output.glob("editorial-*.json"))]
    calls = output / "ai-calls.jsonl"
    ai = [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()
          if line.strip()] if calls.is_file() else []
    return {"run_directory": output.name, "status": report.get("status", "incomplete"),
        "discarded_without_ai": ingest.get("discarded_before_article_fetch", 0)
            + ingest.get("discarded_before_persistence", 0) + triage.get("discarded", 0),
        "channel_topics": len(topics.get("topics", [])),
        "channel_topics_deferred": sum(t["status"] == "deferred_missing_primary_source"
                                       for t in topics.get("topics", [])),
        "source_failures": ingest.get("failures", 0),
        "channel_failures": ingest.get("channel_failures", 0),
        "ai_calls": len(ai), "ai_failures": [c for c in ai if c["status"] == "failed"],
        "editorial_decisions": decisions, "sent_message_ids": report.get("sent_message_ids", []),
        "remaining_count": report.get("remaining_count"),
        "note": ("Отложено по правилам или без первоисточника — "
                 "не означает активную проверку человеком.")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", required=True, type=Path)
    args = parser.parse_args()
    outputs = sorted(p for p in args.runs.iterdir() if p.is_dir())
    if not outputs:
        raise ValueError("no runs found")
    print(json.dumps(summarize(outputs[-1]), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
