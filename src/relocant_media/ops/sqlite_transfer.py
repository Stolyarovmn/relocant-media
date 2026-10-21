"""Verified read-only PostgreSQL export preserving all relational IDs and receipts."""
import argparse
import hashlib
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from relocant_media.storage import ad_review as ad_review_models  # noqa: F401
from relocant_media.storage import correction_control as correction_control_models  # noqa: F401
from relocant_media.storage import revision_control as revision_control_models  # noqa: F401
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import Base, Source
from relocant_media.storage.sqlite_schema import initialize_sqlite, verify_sqlite_schema


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("source timestamp lacks timezone")
        return {"datetime": value.astimezone(UTC).isoformat()}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, dict):
        return {key: _value(member) for key, member in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(member) for member in value]
    return value


def fingerprint(connection: Connection) -> dict[str, Any]:
    result = {}
    for table in Base.metadata.sorted_tables:
        digest, count = hashlib.sha256(), 0
        rows = connection.execute(select(table).order_by(*table.primary_key.columns)).mappings()
        for row in rows:
            encoded = json.dumps(_value(dict(row)), sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":"), allow_nan=False)
            digest.update(encoded.encode() + b"\n")
            count += 1
        result[table.name] = {"rows": count, "sha256": digest.hexdigest()}
    return result


def transfer(source: Engine, output: Path, *, city: str = "belgrade") -> dict[str, Any]:
    if source.dialect.name != "postgresql" or output.exists():
        raise ValueError("PostgreSQL and a new output directory required")
    output.mkdir(parents=True, exist_ok=False)
    target_path = output / "editorial.sqlite3"
    initialize_sqlite(target_path)
    target = create_engine(f"sqlite:///{target_path.resolve().as_posix()}")
    try:
        # Obtain the writer boundary BEFORE opening the repeatable-read snapshot.
        with Session(source) as barrier:
            lock_editorial_city(barrier, city)
            with source.connect().execution_options(isolation_level="REPEATABLE READ") as reader:
                reader.execute(text("SET TRANSACTION READ ONLY"))
                cities = set(reader.scalars(select(Source.city).distinct()))
                if not cities <= {city}:
                    raise ValueError("source contains cities outside the locked scope")
                inspector = inspect(reader)
                tables = set(inspector.get_table_names())
                if tables != set(Base.metadata.tables) | {"alembic_version"}:
                    raise ValueError("unmodeled source tables require a migration plan")
                for table in Base.metadata.sorted_tables:
                    columns = {column["name"] for column in inspector.get_columns(table.name)}
                    if columns != set(table.columns.keys()):
                        raise ValueError("unmodeled source columns require a migration plan")
                before = fingerprint(reader)
                floors = {}
                with target.begin() as writer:
                    for table in Base.metadata.sorted_tables:
                        rows = reader.execute(select(table)).mappings()
                        for batch in rows.partitions(500):
                            writer.execute(table.insert(), [dict(row) for row in batch])
                        if any(c.autoincrement is True for c in table.primary_key.columns):
                            sequence = reader.scalar(text(
                                "SELECT pg_get_serial_sequence(:table_name, 'id')"),
                                {"table_name": table.name})
                            if sequence:
                                sequence_name = sequence.split(".")[-1].strip('"')
                                floor = reader.scalar(text(
                                    "SELECT last_value FROM pg_sequences WHERE "
                                    "schemaname=current_schema() AND sequencename=:sequence"),
                                    {"sequence": sequence_name})
                                if floor is not None:
                                    writer.execute(text(
                                        "DELETE FROM sqlite_sequence WHERE name=:name"),
                                        {"name": table.name})
                                    writer.execute(text(
                                        "INSERT INTO sqlite_sequence(name,seq) "
                                        "VALUES(:name,:floor)"),
                                        {"name": table.name, "floor": floor})
                                    floors[table.name] = floor
                    after = fingerprint(writer)
                    if before != after:
                        raise ValueError("transfer changed relational data")
                reader.rollback()
            barrier.rollback()
        verify_sqlite_schema(target)
        report = {"kind": "verified-postgresql-to-sqlite", "verified": True,
                  "created_at": datetime.now(UTC).isoformat(), "tables": before,
                  "sequence_floors": floors, "database": target_path.name,
                  "source_read_only": True, "activated": False}
        with (output / "transfer.json").open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
        return report
    finally:
        target.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверяемый перенос staging PostgreSQL в SQLite")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL")
    if os.getenv("APP_ENV") != "staging" or not url:
        parser.exit(1, "Нужны явные staging и DATABASE_URL.\n")
    engine = create_engine(url)
    try:
        report = transfer(engine, args.output)
    except Exception:
        parser.exit(1, "Перенос не подтверждён: проверьте журнал, схему и новый каталог.\n")
    finally:
        engine.dispose()
    print(json.dumps({"verified": report["verified"], "tables": report["tables"]}))


if __name__ == "__main__":
    main()
