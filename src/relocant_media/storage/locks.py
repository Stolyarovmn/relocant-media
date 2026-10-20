"""Common transaction boundary for source snapshots and editorial decisions."""
import hashlib
from sqlite3 import Connection as SQLiteConnection

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def lock_editorial_city(session: Session, city: str) -> None:
    if session.get_bind().dialect.name == "sqlite":
        connection = session.connection()
        driver = connection.connection.driver_connection
        if not isinstance(driver, SQLiteConnection):
            raise ValueError("unsupported SQLite driver")
        # SQLite has one writer for the whole file. Acquire it before reading
        # evidence; a plain SELECT's deferred transaction is insufficient.
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
        else:
            # Promote an explicitly opened read transaction or fail closed if
            # its snapshot can no longer acquire the SQLite writer reservation.
            connection.exec_driver_sql("UPDATE source SET id = id WHERE 0")
        return
    digest = hashlib.sha256(f"editorial-events:{city}".encode()).digest()
    key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    session.execute(select(func.pg_advisory_xact_lock(key)))
