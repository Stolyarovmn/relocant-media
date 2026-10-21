import sqlite3

from sqlalchemy import create_engine

from relocant_media.storage.models import Base
from relocant_media.storage.sqlite_schema import SQLITE_SCHEMA_VERSION, initialize_sqlite


def test_v5_schema_upgrades_to_v6_with_backup_and_adds_lifecycle_table(tmp_path) -> None:
    path = tmp_path / "v5.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE source_revision_materialization")
        connection.execute("PRAGMA user_version=5")
        connection.execute(
            "INSERT INTO source(id,city,name,url,source_type,trust,enabled) "
            "VALUES('s','belgrade','Official','https://example.test','official',1,1)"
        )
        connection.execute(
            "INSERT INTO raw_item(id,source_id,external_id,url,title,body) "
            "VALUES(31,'s','article','https://example.test/31','Title','Body')"
        )

    backup = initialize_sqlite(path)
    assert backup is not None and backup.name.endswith(".before-v6.backup")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("SELECT id FROM raw_item").fetchone() == (31,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='source_revision_materialization'"
        ).fetchone() is None

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SQLITE_SCHEMA_VERSION
        assert connection.execute("SELECT id FROM raw_item").fetchone() == (31,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='source_revision_materialization'"
        ).fetchone() == ("source_revision_materialization",)
