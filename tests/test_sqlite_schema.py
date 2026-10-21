import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine

from relocant_media.storage.models import Base
from relocant_media.storage.primary_lock import primary_lock
from relocant_media.storage.sqlite_schema import ADDITIONS, initialize_sqlite, verify_sqlite_schema


def legacy_database(path):
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ix_raw_item_canonical_url")
        connection.execute("DROP INDEX ix_raw_item_content_sha256")
        for table, columns in ADDITIONS.items():
            for column in columns:
                connection.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        connection.execute("DROP TABLE reader_correction_decision")
        connection.execute("DROP TABLE source_revision_decision")
        connection.execute("DROP TABLE source_revision")
        connection.execute("DROP TABLE experiment_assignment")
        connection.execute("DROP TABLE editorial_experiment")
        connection.execute("INSERT INTO source(id,city,name,url,source_type,trust,enabled) "
                           "VALUES('s','belgrade','Official','https://example.test','official',1,1)")
        connection.execute("INSERT INTO raw_item(id,source_id,external_id,url,title,body) "
                           "VALUES(1,'s','article','https://example.test/a','Title','Original')")
        connection.execute("INSERT INTO post(id,city,category,status,title,body,confidence,"
                           "telegram_message_id) VALUES(3,'belgrade','events','published',"
                           "'Пост','Текст',0.99,9)")
        connection.execute("INSERT INTO editorial_run(raw_item_id,post_id,outcome,evidence,"
                           "delivery_status) VALUES(1,3,'{}','{}','sent')")


def test_legacy_schema_keeps_ids_receipts_and_backup(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy_database(path)
    backup = initialize_sqlite(path, legacy_utc_confirmed=True)
    assert backup is not None and backup.is_file()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT id,telegram_message_id,editorial_rule_version FROM post"
                                  ).fetchone() == (3, 9, "legacy-v1")
        assert connection.execute(
            "SELECT delivery_status FROM editorial_run").fetchone() == ("sent",)
        assert connection.execute("SELECT body FROM raw_item").fetchone() == ('Original',)
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "editorial_experiment",
            "experiment_assignment",
            "source_revision_decision",
            "reader_correction_decision",
        } <= tables
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert initialize_sqlite(path) is None


def test_v1_schema_upgrades_to_v4_with_backup_and_keeps_queue(tmp_path):
    path = tmp_path / "v1.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE reader_correction_decision")
        connection.execute("DROP TABLE source_revision_decision")
        connection.execute("DROP TABLE experiment_assignment")
        connection.execute("DROP TABLE editorial_experiment")
        connection.execute("PRAGMA user_version=1")
        connection.execute(
            "INSERT INTO source(id,city,name,url,source_type,trust,enabled) "
            "VALUES('s','belgrade','Official','https://example.test','official',1,1)"
        )
        connection.execute(
            "INSERT INTO raw_item(id,source_id,external_id,url,title,body) "
            "VALUES(7,'s','article','https://example.test/7','Title','Body')"
        )

    backup = initialize_sqlite(path)
    assert backup is not None and backup.name.endswith(".before-v2.backup")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT id FROM raw_item").fetchone() == (7,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='editorial_experiment'"
        ).fetchone() is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT id FROM raw_item").fetchone() == (7,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='editorial_experiment'"
        ).fetchone() == ("editorial_experiment",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='reader_correction_decision'"
        ).fetchone() == ("reader_correction_decision",)


def test_v2_schema_upgrades_to_v4_with_backup_and_keeps_queue(tmp_path):
    path = tmp_path / "v2.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE reader_correction_decision")
        connection.execute("DROP TABLE source_revision_decision")
        connection.execute("PRAGMA user_version=2")
        connection.execute(
            "INSERT INTO source(id,city,name,url,source_type,trust,enabled) "
            "VALUES('s','belgrade','Official','https://example.test','official',1,1)"
        )
        connection.execute(
            "INSERT INTO raw_item(id,source_id,external_id,url,title,body) "
            "VALUES(8,'s','article','https://example.test/8','Title','Body')"
        )

    backup = initialize_sqlite(path)
    assert backup is not None and backup.name.endswith(".before-v3.backup")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("SELECT id FROM raw_item").fetchone() == (8,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='source_revision_decision'"
        ).fetchone() is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT id FROM raw_item").fetchone() == (8,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='source_revision_decision'"
        ).fetchone() == ("source_revision_decision",)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='reader_correction_decision'"
        ).fetchone() == ("reader_correction_decision",)


def test_v3_schema_upgrades_to_v4_with_backup_and_keeps_queue(tmp_path):
    path = tmp_path / "v3.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DROP TABLE reader_correction_decision")
        connection.execute("PRAGMA user_version=3")
        connection.execute(
            "INSERT INTO submission(id,kind,payload,status) "
            "VALUES(12,'correction','{\"city\":\"belgrade\"}','new')"
        )

    backup = initialize_sqlite(path)
    assert backup is not None and backup.name.endswith(".before-v4.backup")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute("SELECT id FROM submission").fetchone() == (12,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='reader_correction_decision'"
        ).fetchone() is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT id FROM submission").fetchone() == (12,)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='reader_correction_decision'"
        ).fetchone() == ("reader_correction_decision",)


def test_ambiguous_legacy_dates_are_not_silently_assigned_utc(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    legacy_database(path)
    with pytest.raises(ValueError, match="timezone requires audit"):
        initialize_sqlite(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "ownership" not in {
            row[1] for row in connection.execute("PRAGMA table_info(source)")}


def test_failed_integrity_check_rolls_back_ddl_and_version(tmp_path):
    path = tmp_path / "broken.sqlite3"
    legacy_database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE raw_item SET source_id='missing'")
    with pytest.raises(ValueError, match="foreign key check"):
        initialize_sqlite(path, legacy_utc_confirmed=True)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert "ownership" not in {
            row[1] for row in connection.execute("PRAGMA table_info(source)")}
        assert connection.execute("SELECT body FROM raw_item").fetchone()[0] == 'Original'


def test_missing_database_is_not_reinitialized_by_verifier(tmp_path):
    path = tmp_path / "absent.sqlite3"
    engine = create_engine(f"sqlite:///{path}")
    with pytest.raises(ValueError, match="existing local"):
        verify_sqlite_schema(engine)
    assert not path.exists()
    engine.dispose()


def test_primary_file_lock_excludes_other_process_and_releases_after_exception(tmp_path):
    path = tmp_path / "editorial.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    code = ("import sys; from sqlalchemy import create_engine; "
            "from relocant_media.storage.primary_lock import primary_lock; "
            "e=create_engine('sqlite:///'+sys.argv[1]); "
            "lock=primary_lock(e,1); print(lock.__enter__(),flush=True); "
            "lock.__exit__(None,None,None); e.dispose()")

    def child():
        result = subprocess.run([sys.executable, '-c', code, str(path)], text=True,
                                capture_output=True, timeout=15, check=True)
        return result.stdout.strip()

    with pytest.raises(RuntimeError):
        with primary_lock(engine, 1) as acquired:
            assert acquired
            assert child() == 'False'
            raise RuntimeError("worker interrupted")
    assert child() == 'True'
    engine.dispose()


def test_checked_snapshot_contains_committed_wal_and_does_not_overwrite(tmp_path):
    import hashlib

    from relocant_media.ops.sqlite_backup_cli import create_snapshot

    path = tmp_path / "live.sqlite3"
    initialize_sqlite(path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO source(id,city,name,url,source_type,trust,enabled) "
                           "VALUES('s','belgrade','Official','https://example.test','official',1,1)")
        connection.commit()
        output = tmp_path / "snapshot"
        manifest = create_snapshot(path, output)
        snapshot = output / manifest["file"]
        assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == manifest["sha256"]
        assert manifest["upload_ready"]
        with sqlite3.connect(snapshot) as restored:
            assert restored.execute("SELECT id FROM source").fetchone()[0] == 's'
            assert restored.execute("PRAGMA user_version").fetchone()[0] == 4
        with pytest.raises(ValueError):
            create_snapshot(path, output)
    finally:
        connection.close()


def test_snapshot_never_creates_missing_source_database(tmp_path):
    from relocant_media.ops.sqlite_backup_cli import create_snapshot

    absent = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError):
        create_snapshot(absent, tmp_path / "backup")
    assert not absent.exists()
