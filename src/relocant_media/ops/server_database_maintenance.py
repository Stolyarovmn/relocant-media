"""Known v7 -> v8 server maintenance; caller retains both inherited locks until deploy ends."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy.engine import make_url

from relocant_media.storage.models import Base
from relocant_media.storage.sqlite_schema import SQLITE_SCHEMA_VERSION


def database_path(value: str) -> Path:
    url = make_url(value)
    if url.get_backend_name() != "sqlite" or not url.database or url.query:
        raise ValueError("existing server SQLite required")
    original = Path(url.database)
    if original.is_symlink():
        raise ValueError("canonical SQLite symlink is not supported")
    path = original.resolve()
    if not path.is_file():
        raise ValueError("existing server SQLite required")
    return path


def require_locks(database: Path, worker_fd: int, intake_fd: int) -> None:
    for descriptor, name in ((worker_fd, "worker.lock"), (intake_fd, "intake-consumer.lock")):
        actual = os.fstat(descriptor)
        expected = (database.parent / name).stat()
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise ValueError("canonical server lock identity differs")
        # Inherited descriptors share the deploy shell's open file descriptions.
        # A different owner's lock cannot be acquired, and the shell retains these
        # locks after this subprocess exits, including throughout rollback.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(path), safe='/')}?mode={'ro' if readonly else 'rw'}",
                           uri=True, isolation_level=None)


def quoted(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def writable_database(database: Path) -> Iterator[sqlite3.Connection]:
    info = database.stat()
    connection = connect(database)
    try:
        yield connection
    finally:
        connection.close()
        # A retained read-only connection can keep root-created WAL/SHM alive.
        # Preserve access for the existing service user without deleting live WAL.
        for suffix in ("-wal", "-shm"):
            try:
                descriptor = os.open(str(database) + suffix, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                continue
            try:
                sidecar = os.fstat(descriptor)
                if not stat.S_ISREG(sidecar.st_mode):
                    raise ValueError("unexpected SQLite sidecar type")
                if (sidecar.st_uid, sidecar.st_gid) != (info.st_uid, info.st_gid):
                    os.fchown(descriptor, info.st_uid, info.st_gid)
                if stat.S_IMODE(sidecar.st_mode) != stat.S_IMODE(info.st_mode):
                    os.fchmod(descriptor, stat.S_IMODE(info.st_mode))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def verify(connection: sqlite3.Connection, version: int) -> None:
    if (SQLITE_SCHEMA_VERSION != 8 or version not in {7, 8}
            or connection.execute("PRAGMA user_version").fetchone()[0] != version):
        raise ValueError("only known SQLite v7 to v8 deployment is supported")
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise ValueError("SQLite integrity check failed")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("SQLite foreign key check failed")
    for table in Base.metadata.sorted_tables:
        present = {row[1] for row in connection.execute(f"PRAGMA table_info({quoted(table.name)})")}
        required = set(table.columns.keys())
        if table.name == "raw_item" and version == 7:
            required.remove("published_at_precision")
            if "published_at_precision" in present:
                raise ValueError("unexpected v7 precision column")
        if not required <= present:
            raise ValueError("SQLite schema is incomplete")


def digest(connection: sqlite3.Connection, *, data_only: bool = False) -> str:
    result = hashlib.sha256()
    if not data_only:
        result.update(repr(connection.execute("PRAGMA user_version").fetchall()).encode())
        for row in connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                                      "ORDER BY type,name"):
            result.update(repr(row).encode())
    tables = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for name in tables:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted(name)})")
                   if not (data_only and name == "raw_item" and row[1] == "published_at_precision")]
        selection = ",".join(quoted(column) for column in columns)
        result.update(repr((name, columns)).encode())
        for row in connection.execute(f"SELECT {selection} FROM {quoted(name)} "
                                      f"ORDER BY {selection}"):
            result.update(repr(row).encode())
    return result.hexdigest()


def sync(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save_receipt(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync(path)


def upgrade(database: Path, receipt: Path, *, worker_fd: int, intake_fd: int) -> None:
    require_locks(database, worker_fd, intake_fd)
    if receipt.exists() or receipt.with_suffix(".tmp").exists():
        raise ValueError("deployment receipt already exists")
    with writable_database(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        verify(connection, version)
        if version == 8:
            return
        info = database.stat()
        backup = receipt.with_suffix(".before-v8.sqlite3")
        descriptor = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        before = digest(connection)
        data_before = digest(connection, data_only=True)
        with closing(sqlite3.connect(backup)) as saved:
            connection.backup(saved)
            verify(saved, 7)
            if digest(saved) != before:
                raise ValueError("backup readback differs")
        sync(backup)
        record = {"version": 1, "database": str(database), "backup": str(backup),
                  "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
                  "before": before, "uid": info.st_uid, "gid": info.st_gid,
                  "mode": stat.S_IMODE(info.st_mode), "status": "prepared"}
        save_receipt(receipt, record)
        connection.execute("PRAGMA synchronous=FULL")
        try:
            connection.execute("BEGIN IMMEDIATE")
            verify(connection, 7)
            if digest(connection) != before:
                raise ValueError("database changed after pre-deploy backup")
            connection.execute("ALTER TABLE raw_item ADD COLUMN published_at_precision "
                               "TEXT NOT NULL DEFAULT 'unknown'")
            connection.execute("PRAGMA user_version=8")
            verify(connection, 8)
            if digest(connection, data_only=True) != data_before:
                raise ValueError("migration changed existing data or delivery receipts")
            # Persist both expected states before commit: rollback remains safe even
            # if this process is interrupted immediately before/after SQLite COMMIT.
            record.update(status="upgrading", after=digest(connection))
            save_receipt(receipt, record)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        sync(database)
        record["status"] = "upgraded"
        save_receipt(receipt, record)


def rollback(database: Path, receipt: Path, *, worker_fd: int, intake_fd: int) -> None:
    require_locks(database, worker_fd, intake_fd)
    record = json.loads(receipt.read_text())
    if record.get("version") != 1 or record.get("database") != str(database):
        raise ValueError("rollback receipt does not match canonical database")
    backup = Path(record["backup"])
    if (backup != receipt.with_suffix(".before-v8.sqlite3") or not backup.is_file()
            or hashlib.sha256(backup.read_bytes()).hexdigest() != record["backup_sha256"]):
        raise ValueError("rollback backup hash differs")
    with closing(connect(backup, readonly=True)) as saved, writable_database(database) as current:
        verify(saved, 7)
        if digest(saved) != record["before"]:
            raise ValueError("rollback backup content differs")
        actual = digest(current)
        if actual == record["before"]:
            pass
        elif actual == record.get("after"):
            saved.backup(current)
        else:
            raise ValueError("database changed since migration; automatic rollback refused")
        verify(current, 7)
        if digest(current) != record["before"]:
            raise ValueError("rollback verification failed")
    info = database.stat()
    if (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (
            record["uid"], record["gid"], record["mode"]):
        raise ValueError("database ownership or mode changed")
    sync(database)
    record["status"] = "rolled_back"
    save_receipt(receipt, record)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["upgrade", "rollback"])
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--worker-fd", type=int, default=9)
    parser.add_argument("--intake-fd", type=int, default=8)
    args = parser.parse_args()
    try:
        database = database_path(os.environ["DATABASE_URL"])
        operation = upgrade if args.mode == "upgrade" else rollback
        operation(database, args.receipt, worker_fd=args.worker_fd, intake_fd=args.intake_fd)
    except Exception as error:
        parser.exit(1, f"server database maintenance blocked: {type(error).__name__}\n")
    print(f"server database maintenance: {args.mode} verified")


if __name__ == "__main__":
    main()
