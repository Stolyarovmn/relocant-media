"""Export ingested media candidates into a fail-closed cover review packet."""

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.media_review_packet import (
    build_media_review_packet,
    pending_media_review_ids,
    plan_reviewed_packet,
)
from relocant_media.storage.sqlite_schema import verify_sqlite_schema


def run(
    *,
    raw_item_id: int | None,
    output: Path,
    assessments: Path | None = None,
    event_year: int | None = None,
    allow_album: bool = False,
) -> dict[str, object]:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging required")
    if output.exists():
        raise ValueError("new output directory required")
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise ValueError("explicit DATABASE_URL required")
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database or url.query:
        raise ValueError("existing local SQLite required")
    database = Path(url.database)
    if not database.is_file():
        raise ValueError("existing local SQLite required")
    if assessments is not None and event_year is None:
        raise ValueError("event year required with assessments")

    engine = create_engine(url)
    try:
        verify_sqlite_schema(engine)
        with Session(engine) as session:
            selected_id = raw_item_id
            if selected_id is None:
                pending = pending_media_review_ids(session, limit=1)
                if not pending:
                    return {
                        "mode": "text",
                        "reason": "media_review_queue_empty",
                        "raw_item_id": None,
                        "publication_permitted": False,
                        "download_permitted": False,
                    }
                selected_id = pending[0]
            packet = build_media_review_packet(session, selected_id)

        output.mkdir(parents=True, exist_ok=False)
        packet_path = output / "packet.json"
        packet_path.write_text(
            json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        templates = [item["assessment_template"] for item in packet.get("candidates", [])]
        (output / "assessment-template.json").write_text(
            json.dumps(templates, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        plan: dict[str, object] | None = None
        if assessments is not None:
            values = json.loads(assessments.read_text(encoding="utf-8"))
            if not isinstance(values, list):
                raise ValueError("assessment file must contain a list")
            reviewed = tuple(CoverAssessment.model_validate(value) for value in values)
            assert event_year is not None
            plan = plan_reviewed_packet(
                packet,
                reviewed,
                event_year=event_year,
                allow_album=allow_album,
            )
            (output / "cover-plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return {
            "mode": packet["mode"],
            "raw_item_id": selected_id,
            "candidate_count": len(packet.get("candidates", [])),
            "plan_mode": plan["mode"] if plan is not None else None,
            "publication_permitted": False,
            "download_permitted": False,
        }
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export READY editorial media candidates for explicit cover review",
    )
    parser.add_argument("--raw-item-id", type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--assessments", type=Path)
    parser.add_argument("--event-year", type=int)
    parser.add_argument("--allow-album", action="store_true")
    args = parser.parse_args()
    try:
        result = run(**vars(args))
    except Exception:
        parser.exit(1, "Медиа-ревью остановлено: проверьте READY-пакет и assessment input.\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
