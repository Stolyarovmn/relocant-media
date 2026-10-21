"""Offline/read-only Telegram Ads snapshot analysis CLI."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from relocant_media.ads.analysis import AdsAnalysisPolicy, CampaignSnapshot, analyze_ads


@dataclass(frozen=True, slots=True)
class JsonSnapshotSource:
    snapshots: list[CampaignSnapshot]

    def read_snapshots(self) -> list[CampaignSnapshot]:
        return list(self.snapshots)


def _campaign(row: dict[str, Any]) -> CampaignSnapshot:
    required = {
        "campaign_id",
        "name",
        "status",
        "currency",
        "captured_at",
        "spend",
        "impressions",
        "clicks",
        "joins",
    }
    missing = required - row.keys()
    if missing:
        raise ValueError(f"missing campaign fields: {sorted(missing)}")
    captured_at = datetime.fromisoformat(str(row["captured_at"]).replace("Z", "+00:00"))
    if captured_at.tzinfo is None:
        raise ValueError("captured_at must include timezone")
    return CampaignSnapshot(
        campaign_id=str(row["campaign_id"]),
        name=str(row["name"]),
        status=str(row["status"]),
        currency=str(row["currency"]),
        captured_at=captured_at,
        spend=Decimal(str(row["spend"])),
        impressions=int(row["impressions"]),
        clicks=int(row["clicks"]),
        joins=int(row["joins"]),
    )


def load_source(path: Path) -> JsonSnapshotSource:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("campaigns"), list):
        raise ValueError("input must be an object with campaigns list")
    snapshots: list[CampaignSnapshot] = []
    for row in payload["campaigns"]:
        if not isinstance(row, dict):
            raise ValueError("each campaign snapshot must be an object")
        snapshots.append(_campaign(row))
    return JsonSnapshotSource(snapshots)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Analyze exported Telegram Ads snapshots without mutation capability"
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target-cost-per-join", required=True, type=Decimal)
    parser.add_argument("--minimum-impressions", type=int, default=1000)
    parser.add_argument("--minimum-joins", type=int, default=3)
    parser.add_argument("--high-cost-multiplier", type=Decimal, default=Decimal("1.5"))
    return parser


def main() -> None:
    args = build_parser().parse_args()
    source = load_source(args.input)
    report = analyze_ads(
        source,
        policy=AdsAnalysisPolicy(
            target_cost_per_join=args.target_cost_per_join,
            minimum_impressions=args.minimum_impressions,
            minimum_joins_for_efficiency_decision=args.minimum_joins,
            high_cost_multiplier=args.high_cost_multiplier,
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
