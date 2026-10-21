"""Validate an immutable snapshot before restoring into a fresh local directory."""
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine

from relocant_media.storage.sqlite_schema import SQLITE_SCHEMA_VERSION, verify_sqlite_schema


def restore(snapshot: Path, manifest_path: Path, output: Path) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (
        manifest.get("kind") != "sqlite-backup"
        or manifest.get("upload_ready") is not True
        or manifest.get("schema_version") != SQLITE_SCHEMA_VERSION
        or not snapshot.is_file()
        or output.exists()
    ):
        raise ValueError("valid snapshot and new restore directory required")
    with snapshot.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if (
        digest != manifest.get("sha256")
        or snapshot.stat().st_size != manifest.get("bytes")
    ):
        raise ValueError("snapshot checksum/size mismatch")
    output.mkdir(parents=True, exist_ok=False)
    target = output / "editorial.sqlite3"
    with snapshot.open("rb") as source, target.open("xb") as destination:
        shutil.copyfileobj(source, destination)
    with target.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError("restored bytes differ")
    engine = create_engine(f"sqlite:///{target.resolve().as_posix()}")
    try:
        verify_sqlite_schema(engine)
    finally:
        engine.dispose()
    result = {"verified": True, "sha256": digest, "activated": False}
    with (output / "restore.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    return result
