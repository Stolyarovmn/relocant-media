import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from relocant_media.storage.sqlite_schema import initialize_sqlite


def test_fresh_process_initialization_registers_media_snapshot_model(tmp_path):
    script = """
import sqlite3, sys
from pathlib import Path
from relocant_media.storage.sqlite_schema import initialize_sqlite
path = Path(sys.argv[1])
initialize_sqlite(path)
with sqlite3.connect(path) as db:
    query = "SELECT name FROM sqlite_master WHERE name='raw_item_media_snapshot'"
    assert db.execute(query).fetchone()
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    subprocess.run([sys.executable, "-c", script, str(tmp_path / "fresh.sqlite3")],
                   env=env, check=True, capture_output=True, text=True)


def test_version_six_adds_media_snapshot_and_keeps_existing_rows(tmp_path):
    path = tmp_path / "queue.sqlite3"
    initialize_sqlite(path)
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE raw_item_media_snapshot")
        db.execute("PRAGMA user_version=6")
        db.execute("INSERT INTO post(id,city,category,status,title,body,confidence,"
                   "telegram_message_id) VALUES(24,'belgrade','events','published',"
                   "'Заголовок','Текст',0.99,24)")
        before = db.execute("SELECT * FROM post").fetchall()
    backup = initialize_sqlite(path)
    assert backup == path.with_name(path.name + ".before-v7.backup")
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 7
        assert db.execute("SELECT * FROM post").fetchall() == before
        assert db.execute("SELECT count(*) FROM raw_item_media_snapshot").fetchone()[0] == 0
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 6
        assert db.execute("SELECT * FROM post").fetchall() == before
