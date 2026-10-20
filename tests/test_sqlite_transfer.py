import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session
from test_editorial_integration import prepare_service

from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.ops.sqlite_restore import restore
from relocant_media.ops.sqlite_transfer import fingerprint, transfer
from relocant_media.storage.models import RawItem

editorial_session = _editorial_session


def test_transfer_and_restore_preserve_all_rows_receipts_and_sequence_floor(
    editorial_session: Session, tmp_path,
):
    service, _, raw_id = prepare_service(editorial_session, category="transport")
    run = service.prepare(raw_id)
    run.delivery_status = "uncertain"
    editorial_session.commit()
    # Deleted IDs must not become available again after moving the queue.
    editorial_session.execute(text("SELECT setval(pg_get_serial_sequence('raw_item','id'), 300)"))
    editorial_session.commit()
    engine = editorial_session.get_bind()
    report = transfer(engine, tmp_path / "transfer")
    assert report["verified"] and not report["activated"]
    target_path = tmp_path / "transfer" / "editorial.sqlite3"
    target = create_engine(f"sqlite:///{target_path}")
    with engine.connect() as source, target.connect() as copy:
        assert fingerprint(source) == fingerprint(copy)
    with Session(target) as session:
        item = session.get(RawItem, raw_id)
        session.add(RawItem(source_id=item.source_id, external_id="next",
                            url="https://example.test/next", title="Next", body="Next body"))
        session.commit()
        assert session.scalar(text("SELECT max(id) FROM raw_item")) > 300
    target.dispose()
    snapshot_dir = tmp_path / "snapshot"
    manifest = create_snapshot(target_path, snapshot_dir)
    restored = tmp_path / "restored"
    assert restore(snapshot_dir / "editorial.sqlite3", snapshot_dir / "manifest.json",
                   restored)["verified"]
    manifest["sha256"] = "0" * 64
    (snapshot_dir / "bad.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum"):
        restore(snapshot_dir / "editorial.sqlite3", snapshot_dir / "bad.json", tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_transfer_rejects_unmodeled_source_column(editorial_session: Session, tmp_path):
    prepare_service(editorial_session)
    editorial_session.execute(text("ALTER TABLE source ADD COLUMN transfer_test_extra TEXT"))
    editorial_session.commit()
    try:
        with pytest.raises(ValueError, match="unmodeled source columns"):
            transfer(editorial_session.get_bind(), tmp_path / "blocked")
        assert not (tmp_path / "blocked" / "transfer.json").exists()
    finally:
        editorial_session.execute(text("ALTER TABLE source DROP COLUMN transfer_test_extra"))
        editorial_session.commit()
