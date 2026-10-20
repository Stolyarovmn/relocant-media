"""Cross-commit primary lock: dedicated PostgreSQL session or local file lock."""
import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import Engine


@contextmanager
def primary_lock(engine: Engine, key: int) -> Iterator[bool]:
    if engine.dialect.name == "sqlite":
        database = engine.url.database
        if not database or database == ":memory:" or database.startswith("file:"):
            raise ValueError("primary requires a persistent local SQLite file")
        path = Path(database).resolve()
        with path.with_name(path.name + ".primary.lock").open("a+b") as handle:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            module = importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")
            try:
                if os.name == "nt":
                    module.locking(handle.fileno(), module.LK_NBLCK, 1)
                else:
                    module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                handle.seek(0)
                if os.name == "nt":
                    module.locking(handle.fileno(), module.LK_UNLCK, 1)
                else:
                    module.flock(handle.fileno(), module.LOCK_UN)
        return
    if engine.dialect.name != "postgresql":
        raise ValueError("unsupported primary database")
    with engine.connect() as connection:
        acquired = connection.scalar(select(func.pg_try_advisory_lock(key)))
        connection.commit()
        try:
            yield bool(acquired)
        finally:
            if acquired:
                try:
                    connection.execute(select(func.pg_advisory_unlock(key)))
                    connection.commit()
                except Exception:
                    connection.invalidate()
                    raise
