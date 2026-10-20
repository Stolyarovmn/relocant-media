"""Common transaction boundary for source snapshots and editorial decisions."""
import hashlib

from sqlalchemy import func, select
from sqlalchemy.orm import Session


def lock_editorial_city(session: Session, city: str) -> None:
    digest = hashlib.sha256(f"editorial-events:{city}".encode()).digest()
    key = int.from_bytes(digest[:8], byteorder="big", signed=True)
    session.execute(select(func.pg_advisory_xact_lock(key)))
