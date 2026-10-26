import fcntl
import json
import sqlite3
import stat
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from relocant_media.ops import server_database_maintenance as maintenance
from relocant_media.storage.sqlite_schema import initialize_sqlite


@pytest.fixture
def database(tmp_path):
    path = tmp_path / 'editorial.sqlite3'
    initialize_sqlite(path)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE raw_item DROP COLUMN published_at_precision')
        connection.execute('PRAGMA user_version=7')
        connection.execute("INSERT INTO source(id,city,name,url,source_type,trust,enabled) "
                           "VALUES('s','belgrade','Official','https://example.test','official',1,1)")
        for item_id, status in enumerate(('sent', 'sending', 'uncertain'), 1):
            connection.execute("INSERT INTO raw_item(id,source_id,external_id,url,title,body) "
                "VALUES(?,'s',?,'https://example.test','Title','Original')",
                (item_id, str(item_id)))
            connection.execute("INSERT INTO post(id,city,category,status,title,body,confidence,"
                "telegram_message_id) VALUES(?,'belgrade','city','published','Пост','Текст',1,?)",
                (item_id, 900 + item_id))
            connection.execute("INSERT INTO editorial_run(raw_item_id,post_id,outcome,evidence,"
                "delivery_status) VALUES(?,?,'{}','{}',?)", (item_id, item_id, status))
        connection.execute("INSERT INTO telegram_update_receipt(update_id,kind,status) "
                           "VALUES(12345,'message','processed')")
        connection.execute("INSERT INTO submission(id,kind,payload,status) "
                           "VALUES(5,'news_tip','{\"text\":\"PRIVATE\"}','new')")
    path.chmod(0o640)
    return path


@contextmanager
def locks(database):
    with ExitStack() as stack:
        descriptors = {}
        for field, name in (('worker_fd', 'worker.lock'), ('intake_fd', 'intake-consumer.lock')):
            stream = stack.enter_context((database.parent / name).open('a'))
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            descriptors[field] = stream.fileno()
        yield descriptors


def test_known_upgrade_retains_all_data_ids_receipts_and_private_backup(database):
    receipt = database.parent / 'deploy.json'
    info = database.stat()
    with locks(database) as descriptors:
        with maintenance.connect(database) as connection:
            original = maintenance.digest(connection, data_only=True)
        maintenance.upgrade(database, receipt, **descriptors)
        with maintenance.connect(database) as connection:
            maintenance.verify(connection, 8)
            assert maintenance.digest(connection, data_only=True) == original
            assert connection.execute('SELECT published_at_precision FROM raw_item').fetchall() == [
                ('unknown',)] * 3
            assert connection.execute('SELECT telegram_message_id FROM post ORDER BY id').fetchall(
                ) == [(901,), (902,), (903,)]
            assert connection.execute('SELECT delivery_status FROM editorial_run '
                'ORDER BY raw_item_id').fetchall() == [('sent',), ('sending',), ('uncertain',)]
            assert connection.execute('SELECT update_id FROM telegram_update_receipt').fetchall(
                ) == [(12345,)]
        record = json.loads(receipt.read_text())
        backup = Path(record['backup'])
        assert record['status'] == 'upgraded'
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
        current_info = database.stat()
        assert (current_info.st_uid, current_info.st_gid, stat.S_IMODE(current_info.st_mode)
                ) == (info.st_uid, info.st_gid, 0o640)
        with maintenance.connect(backup, readonly=True) as connection:
            maintenance.verify(connection, 7)
        maintenance.rollback(database, receipt, **descriptors)
        assert json.loads(receipt.read_text())['status'] == 'rolled_back'
        with maintenance.connect(database) as connection:
            maintenance.verify(connection, 7)
            assert maintenance.digest(connection) == record['before']
        # A second attempt uses a unique receipt/backup; the former backup stays immutable.
        maintenance.upgrade(database, database.parent / 'retry.json', **descriptors)
        assert backup.is_file()


@pytest.mark.parametrize('version', [0, 6, 9])
def test_unknown_versions_fail_before_backup_or_mutation(database, version):
    with sqlite3.connect(database) as connection:
        connection.execute(f'PRAGMA user_version={version}')
    before = database.read_bytes()
    receipt = database.parent / 'deploy.json'
    with locks(database) as descriptors, pytest.raises(ValueError, match='only known'):
        maintenance.upgrade(database, receipt, **descriptors)
    assert database.read_bytes() == before and not receipt.exists()
    assert not receipt.with_suffix('.before-v8.sqlite3').exists()


def test_v8_verification_is_nonmutating_and_does_not_reserve_backup(database):
    with locks(database) as descriptors:
        maintenance.upgrade(database, database.parent / 'first.json', **descriptors)
        before = database.read_bytes()
        maintenance.upgrade(database, database.parent / 'noop.json', **descriptors)
    assert database.read_bytes() == before
    assert not (database.parent / 'noop.json').exists()


def test_missing_file_is_not_initialized_and_symlinks_are_rejected(tmp_path, database):
    path = tmp_path / 'absent.sqlite3'
    with pytest.raises(ValueError):
        maintenance.database_path(f'sqlite:///{path}')
    assert not path.exists()
    link = tmp_path / 'alias.sqlite3'
    link.symlink_to(database)
    with pytest.raises(ValueError, match='symlink'):
        maintenance.database_path(f'sqlite:///{link}')


def test_wrong_lock_identity_prevents_backup_or_database_change(database):
    (database.parent / 'worker.lock').touch()
    (database.parent / 'intake-consumer.lock').touch()
    other = database.parent / 'other'
    other.mkdir()
    with locks(other / 'other.sqlite3') as descriptors, pytest.raises(ValueError, match='identity'):
        maintenance.upgrade(database, database.parent / 'deploy.json', **descriptors)
    assert not (database.parent / 'deploy.json').exists()


def test_after_migration_writes_are_never_erased_by_automatic_rollback(database):
    receipt = database.parent / 'deploy.json'
    with locks(database) as descriptors:
        maintenance.upgrade(database, receipt, **descriptors)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE editorial_run SET delivery_status='sent' "
                               "WHERE raw_item_id=2")
        with pytest.raises(ValueError, match='changed since migration'):
            maintenance.rollback(database, receipt, **descriptors)
        with maintenance.connect(database) as connection:
            assert connection.execute('SELECT delivery_status FROM editorial_run '
                'WHERE raw_item_id=2').fetchone() == ('sent',)


def test_corrupt_backup_cannot_be_used_for_rollback(database):
    receipt = database.parent / 'deploy.json'
    with locks(database) as descriptors:
        maintenance.upgrade(database, receipt, **descriptors)
        Path(json.loads(receipt.read_text())['backup']).write_bytes(b'corrupt')
        with pytest.raises(ValueError, match='hash differs'):
            maintenance.rollback(database, receipt, **descriptors)
        with maintenance.connect(database) as connection:
            maintenance.verify(connection, 8)


def test_interrupted_precommit_transaction_rolls_back_and_backup_remains(database, monkeypatch):
    receipt = database.parent / 'deploy.json'
    original_save = maintenance.save_receipt
    def fail(path, record):
        original_save(path, record)
        if record['status'] == 'upgrading':
            raise KeyboardInterrupt('before commit')
    monkeypatch.setattr(maintenance, 'save_receipt', fail)
    with locks(database) as descriptors:
        with pytest.raises(KeyboardInterrupt):
            maintenance.upgrade(database, receipt, **descriptors)
        with maintenance.connect(database) as connection:
            maintenance.verify(connection, 7)
        monkeypatch.setattr(maintenance, 'save_receipt', original_save)
        maintenance.rollback(database, receipt, **descriptors)
        assert json.loads(receipt.read_text())['status'] == 'rolled_back'


def test_interrupted_after_commit_uses_precommitted_recovery_fingerprint(database, monkeypatch):
    receipt = database.parent / 'deploy.json'
    original_save = maintenance.save_receipt
    def fail(path, record):
        if record['status'] == 'upgraded':
            raise RuntimeError('after database commit')
        original_save(path, record)
    monkeypatch.setattr(maintenance, 'save_receipt', fail)
    with locks(database) as descriptors:
        with pytest.raises(RuntimeError):
            maintenance.upgrade(database, receipt, **descriptors)
        assert json.loads(receipt.read_text())['status'] == 'upgrading'
        with maintenance.connect(database) as connection:
            maintenance.verify(connection, 8)
        monkeypatch.setattr(maintenance, 'save_receipt', original_save)
        maintenance.rollback(database, receipt, **descriptors)
        with maintenance.connect(database) as connection:
            maintenance.verify(connection, 7)


def test_retained_wal_reader_preserves_sidecar_service_permissions(database):
    reader = None
    with sqlite3.connect(database) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        reader = maintenance.connect(database, readonly=True)
        reader.execute('SELECT count(*) FROM raw_item').fetchone()
    try:
        sidecars = [Path(str(database) + suffix) for suffix in ('-wal', '-shm')]
        assert all(path.is_file() for path in sidecars)
        for path in sidecars:
            path.chmod(0o600)
        with locks(database) as descriptors:
            receipt = database.parent / 'wal-deploy.json'
            maintenance.upgrade(database, receipt, **descriptors)
            for path in sidecars:
                assert path.stat().st_uid == database.stat().st_uid
                assert path.stat().st_gid == database.stat().st_gid
                assert stat.S_IMODE(path.stat().st_mode) == 0o640
            maintenance.rollback(database, receipt, **descriptors)
            for path in sidecars:
                assert stat.S_IMODE(path.stat().st_mode) == 0o640
    finally:
        reader.close()
