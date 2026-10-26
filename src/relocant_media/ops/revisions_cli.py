"""Export source changes for fresh reviews; never approve or send anything."""
import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from relocant_media.storage.models import RawItem, Source, SourceRevision
from relocant_media.storage.sqlite_schema import verify_sqlite_schema


def export_revisions(*, city: str, output: Path) -> dict[str, Any]:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging is required")
    url = os.environ.get("DATABASE_URL")
    if not url or not city.strip() or output.exists():
        raise ValueError("explicit database, city and new output directory required")
    engine = create_engine(url)
    try:
        if engine.dialect.name == "postgresql":
            engine = engine.execution_options(isolation_level="REPEATABLE READ")
        elif engine.dialect.name == "sqlite":
            verify_sqlite_schema(engine)
        else:
            raise ValueError("unsupported export database")
        with Session(engine) as session, session.begin():
            if engine.dialect.name == "sqlite":
                session.execute(text("PRAGMA query_only=ON"))
                session.execute(text("BEGIN"))
            else:
                session.execute(text("SET TRANSACTION READ ONLY"))
            rows = session.execute(select(SourceRevision, RawItem, Source).join(
                RawItem, RawItem.id == SourceRevision.raw_item_id,
            ).join(Source, Source.id == RawItem.source_id).where(
                Source.city == city,
            ).order_by(SourceRevision.observed_at, SourceRevision.id)).all()
            revisions = [{
                "revision_id": revision.id, "raw_item_id": raw.id,
                "snapshot_sha256": revision.snapshot_sha256,
                "observed_at": revision.observed_at.isoformat(),
                "status": "requires_fresh_review", "approval": False,
                "evidence": revision.evidence,
                "previous_evidence": {
                    "source_id": raw.source_id, "article_id": raw.external_id,
                    "url": raw.url, "title": raw.title, "body": raw.body,
                    "published_at": raw.published_at.isoformat() if raw.published_at else None,
                    "published_at_precision": raw.published_at_precision,
                },
                "source_policy": {"enabled": source.enabled, "ownership": source.ownership,
                                  "auto_publish": source.auto_publish,
                                  "high_risk": source.high_risk},
            } for revision, raw, source in rows]
        result = {"version": 1, "city": city, "generated_at": datetime.now(UTC).isoformat(),
                  "revision_count": len(revisions), "revisions": revisions}
        output.mkdir(parents=True, exist_ok=False)
        with (output / "manifest.json").open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, sort_keys=True, indent=2)
        return result
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Экспорт изменений для нового ревью")
    parser.add_argument("--city", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = export_revisions(**vars(args))
    except Exception:
        parser.exit(1, "Экспорт изменений не выполнен: проверьте staging, БД и каталог.\n")
    print(json.dumps({"revision_count": result["revision_count"],
                      "manifest": str(args.output / "manifest.json")}))


if __name__ == "__main__":
    main()
