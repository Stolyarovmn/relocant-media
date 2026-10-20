"""SQLite variants retain PostgreSQL schema types and normalize file dates to UTC."""
from datetime import UTC, datetime
from sqlite3 import Connection as SQLiteConnection

from sqlalchemy import JSON, BigInteger, DateTime, Integer, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect, Engine
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("SQLite dates require an explicit timezone")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


JSON_VALUE = JSONB().with_variant(JSON(), "sqlite")
AUTOINCREMENT_ID = BigInteger().with_variant(Integer(), "sqlite")
UTC_DATE = DateTime(timezone=True).with_variant(UTCDateTime(), "sqlite")


@event.listens_for(Engine, "connect")
def enable_sqlite_constraints(connection: object, record: object) -> None:
    if isinstance(connection, SQLiteConnection):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
