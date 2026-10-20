"""Create an immutable checked snapshot suitable for uploading to Google Drive."""
import argparse
import hashlib
import json
import sqlite3
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from relocant_media.storage.sqlite_schema import SQLITE_SCHEMA_VERSION, verify_sqlite_schema


def create_snapshot(database: Path, output: Path) -> dict[str, Any]:
    database = database.resolve()
    if not database.is_file() or output.exists():
        raise ValueError("existing database and new snapshot directory required")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        verify_sqlite_schema(engine)
    finally:
        engine.dispose()
    output.mkdir(parents=True, exist_ok=False)
    target = output / "editorial.sqlite3"
    with target.open("xb"):
        pass
    deadline = time.monotonic() + 30

    def progress(status: int, remaining: int, total: int) -> None:
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite backup deadline exceeded")

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(target)) as destination:
            source.backup(destination, pages=128, progress=progress)
            if destination.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("snapshot integrity check failed")
            if destination.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("snapshot foreign key check failed")
            version = destination.execute("PRAGMA user_version").fetchone()[0]
            if version != SQLITE_SCHEMA_VERSION:
                raise ValueError("snapshot schema version changed")
    with target.open("rb") as binary_stream:
        digest = hashlib.file_digest(binary_stream, "sha256").hexdigest()
    manifest = {"kind": "sqlite-backup", "schema_version": version,
                "created_at": datetime.now(UTC).isoformat(), "file": target.name,
                "bytes": target.stat().st_size, "sha256": digest,
                "upload_ready": True}
    with (output / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Создать проверенный снимок SQLite для Drive")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = create_snapshot(args.database, args.output)
    except Exception:
        parser.exit(1, "Снимок не готов: проверьте исходную БД и новый каталог.\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
