"""Explicit SQLite initialization/legacy upgrade; never recreate an existing queue."""
import sqlite3
from contextlib import closing
from pathlib import Path

from sqlalchemy import DateTime, create_engine, inspect
from sqlalchemy.engine import Connection, Engine

from relocant_media.scheduler import models as scheduler_models  # noqa: F401
from relocant_media.storage import revision_control as revision_control_models  # noqa: F401
from relocant_media.storage.models import Base

SQLITE_SCHEMA_VERSION = 3
ADDITIONS = {
    "source": {"ownership": "TEXT NOT NULL DEFAULT 'unknown'",
               "high_risk": "BOOLEAN NOT NULL DEFAULT 0",
               "auto_publish": "BOOLEAN NOT NULL DEFAULT 0"},
    "raw_item": {"canonical_url": "TEXT", "content_sha256": "VARCHAR(64)"},
    "post": {"editorial_rule_version": "TEXT NOT NULL DEFAULT 'legacy-v1'"},
    "editorial_run": {"editorial_rule_version": "TEXT NOT NULL DEFAULT 'legacy-v1'"},
}


def _verify_connection(connection: Connection) -> None:
    if connection.exec_driver_sql("PRAGMA user_version").scalar() != SQLITE_SCHEMA_VERSION:
        raise ValueError("SQLite schema requires explicit initialization/upgrade")
    if connection.exec_driver_sql("PRAGMA quick_check").scalar() != "ok":
        raise ValueError("SQLite integrity check failed")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise ValueError("SQLite foreign key check failed")
    inspector = inspect(connection)
    for table in Base.metadata.sorted_tables:
        if not inspector.has_table(table.name):
            raise ValueError("SQLite schema is incomplete")
        present = {column["name"] for column in inspector.get_columns(table.name)}
        if not set(table.columns.keys()) <= present:
            raise ValueError("SQLite columns are incomplete")


def verify_sqlite_schema(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    database = engine.url.database
    if not database or not Path(database).is_file():
        raise ValueError("existing local SQLite file required")
    with engine.connect() as connection:
        _verify_connection(connection)


def _reserve_backup(original: sqlite3.Connection, path: Path) -> Path:
    with path.open("xb"):
        pass
    with closing(sqlite3.connect(path)) as target:
        original.backup(target)
    return path


def initialize_sqlite(path: Path, *, legacy_utc_confirmed: bool = False) -> Path | None:
    """Explicit maintenance operation; all workers must be stopped by the caller."""
    path = path.resolve()
    if not path.parent.is_dir():
        raise ValueError("database parent directory must exist")
    backup = None
    if path.exists():
        with closing(sqlite3.connect(path)) as original:
            version = original.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in original.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if version not in {0, 1, 2, SQLITE_SCHEMA_VERSION}:
                raise ValueError("unsupported SQLite schema version")
            if version == 0 and tables:
                if not {"source", "raw_item", "post", "editorial_run"} <= tables:
                    raise ValueError("unrecognized legacy database")
                if not legacy_utc_confirmed:
                    for table in Base.metadata.sorted_tables:
                        if table.name not in tables:
                            continue
                        legacy_columns = {row[1] for row in original.execute(
                            f'PRAGMA table_info("{table.name}")')}
                        for column in table.columns:
                            if isinstance(column.type, DateTime) and column.name in legacy_columns:
                                if original.execute(
                                    f'SELECT 1 FROM "{table.name}" WHERE "{column.name}" '
                                    'IS NOT NULL LIMIT 1',
                                ).fetchone() is not None:
                                    raise ValueError("legacy timestamp timezone requires audit")
                backup = _reserve_backup(
                    original,
                    path.with_name(path.name + ".before-v1.backup"),
                )
            elif version == 1:
                backup = _reserve_backup(
                    original,
                    path.with_name(path.name + ".before-v2.backup"),
                )
            elif version == 2:
                backup = _reserve_backup(
                    original,
                    path.with_name(path.name + ".before-v3.backup"),
                )
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            version = connection.exec_driver_sql("PRAGMA user_version").scalar()
            if version == 0:
                inspector = inspect(connection)
                for table in Base.metadata.sorted_tables:
                    if inspector.has_table(table.name):
                        present = {c["name"] for c in inspector.get_columns(table.name)}
                        required = set(table.columns.keys()) - set(ADDITIONS.get(table.name, {}))
                        if not required <= present:
                            raise ValueError("unrecognized legacy table columns")
                for table_name, columns in ADDITIONS.items():
                    if inspector.has_table(table_name):
                        present = {column["name"] for column in inspector.get_columns(table_name)}
                        for name, ddl in columns.items():
                            if name not in present:
                                connection.exec_driver_sql(
                                    f'ALTER TABLE "{table_name}" ADD COLUMN "{name}" {ddl}')
                Base.metadata.create_all(connection)
                for table in Base.metadata.tables.values():
                    for index in table.indexes:
                        index.create(connection, checkfirst=True)
                connection.exec_driver_sql(f"PRAGMA user_version={SQLITE_SCHEMA_VERSION}")
            elif version in {1, 2}:
                Base.metadata.create_all(connection)
                for table in Base.metadata.tables.values():
                    for index in table.indexes:
                        index.create(connection, checkfirst=True)
                connection.exec_driver_sql(f"PRAGMA user_version={SQLITE_SCHEMA_VERSION}")
            _verify_connection(connection)
        return backup
    finally:
        engine.dispose()
