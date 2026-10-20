"""Local collection, evidence export and checked backup; no AI or publisher."""
import argparse
import asyncio
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from relocant_media.ops.candidates_cli import aware_datetime, select_candidates
from relocant_media.ops.collect_cli import collect
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.storage.primary_lock import primary_lock
from relocant_media.storage.sqlite_schema import verify_sqlite_schema


def run_local_cycle(
    *, database: Path, output: Path, city: str, since: str, until: str,
    ingest: Callable[[], dict[str, int]],
) -> dict[str, Any]:
    start, end = aware_datetime(since), aware_datetime(until)
    database = database.resolve()
    if not database.is_file() or output.exists() or not city.strip() or start >= end:
        raise ValueError("existing database, new output and valid window required")
    engine = create_engine("sqlite:///" + database.as_posix())
    try:
        verify_sqlite_schema(engine)
        with primary_lock(engine, 0) as acquired:
            if not acquired:
                return {"status": "LOCAL_BUSY", "backup_ready": False}
            output.mkdir(parents=True, exist_ok=False)
            report: dict[str, Any] = {
                "status": "LOCAL_FAILED", "city": city,
                "window": {"since": start.isoformat(), "until": end.isoformat()},
                "backup_ready": False, "remote_backup": "pending",
                "editorial_review": "pending", "telegram_send": False,
            }
            try:
                result = ingest()
                report["collection"] = result
                with Session(engine) as session, session.begin():
                    session.execute(text("PRAGMA query_only=ON"))
                    session.execute(text("BEGIN"))
                    manifest = select_candidates(session, city=city, since=start,
                                                 until=end, limit=50)
                (output / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                report.update(status="LOCAL_PARTIAL" if result["failures"] else "LOCAL_READY",
                              candidate_count=manifest["candidate_count"],
                              eligible_count=manifest["eligible_count"],
                              truncated=manifest["truncated"])
            except Exception as error:
                report["error_type"] = type(error).__name__
            # A partially committed collection must also be backed up. A ready local
            # snapshot is never presented as an uploaded or remotely verified copy.
            try:
                snapshot = create_snapshot(database, output / "snapshot")
                report.update(backup_ready=True, backup_sha256=snapshot["sha256"])
            except Exception as error:
                report.update(status="LOCAL_FAILED", backup_error_type=type(error).__name__)
            (output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return report
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Сбор, кандидаты и снимок SQLite без отправок")
    parser.add_argument("--city", default="belgrade", choices=["belgrade"])
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        url = make_url(os.environ.get("DATABASE_URL", ""))
        if (os.environ.get("APP_ENV") != "staging" or url.get_backend_name() != "sqlite"
                or not url.database or url.database == ":memory:" or url.query
                or url.database.startswith("file:")):
            raise ValueError("explicit local staging SQLite required")
        report = run_local_cycle(database=Path(url.database), output=args.output,
            city=args.city, since=args.since, until=args.until,
            ingest=lambda: asyncio.run(collect(args.city, Path("cities/belgrade/sources"),
                ("city_beograd_news", "cukarica_news"))))
    except Exception:
        parser.exit(1, "Локальный цикл не начат: проверьте SQLite, окно и новый каталог.\n")
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "LOCAL_READY":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
