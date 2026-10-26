import json
import sqlite3
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.domain.freshness import freshness_decision, publication_date_context
from relocant_media.domain.publication_time import publication_interval
from relocant_media.ops.actions_runtime import restored
from relocant_media.ops.candidates_cli import select_candidates
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.storage.models import RawItem, Source
from relocant_media.storage.sqlite_schema import SQLITE_SCHEMA_VERSION, initialize_sqlite

ZONE = ZoneInfo('Europe/Belgrade')


def test_calendar_day_is_an_interval_including_dst_not_an_exact_time():
    day = datetime(2026, 10, 25, tzinfo=ZONE)
    lower, upper = publication_interval(day, 'day')
    assert upper - lower == timedelta(hours=25)
    context = publication_date_context(day, 'day')
    assert context['published_at_belgrade'] is None
    assert context['published_on_belgrade'] == '2026-10-25'
    assert context['published_at_precision'] == 'day'
    assert publication_date_context(day)['published_at_belgrade'] is None


def test_uncertain_freshness_boundary_holds_instead_of_fabricating_freshness():
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)
    result = freshness_decision('Городская новость', 'Объявление',
        datetime(2026, 10, 24, tzinfo=ZONE), now, published_at_precision='day')
    assert result.action == 'hold' and result.reason == 'publication_day_straddles_news_window'
    old = freshness_decision('Городская новость', 'Объявление',
        datetime(2026, 10, 23, tzinfo=ZONE), now, published_at_precision='day')
    assert old.action == 'discard'


def test_late_found_day_only_article_is_not_lost_outside_midnight_window(tmp_path):
    path = tmp_path / 'queue.sqlite3'
    initialize_sqlite(path)
    engine = create_engine(f'sqlite:///{path}')
    since = datetime(2026, 10, 26, 12, tzinfo=UTC)
    with Session(engine) as session:
        session.add(Source(id='official', city='belgrade', name='Official',
                           url='https://example.org', source_type='news', trust=1))
        session.flush()
        session.add(RawItem(source_id='official', external_id='today',
            url='https://example.org/a', title='Книжная встреча', body='Новый городской анонс',
            published_at=datetime(2026, 10, 26, tzinfo=ZONE), published_at_precision='day',
            ingested_at=since + timedelta(minutes=5)))
        session.commit()
        result = select_candidates(session, city='belgrade', since=since,
                                   until=since + timedelta(hours=3), limit=2)
        assert result['candidate_count'] == 1
        row = result['candidates'][0]
        assert row['window_match'] == 'calendar_day_overlap'
        assert row['evidence']['published_at_precision'] == 'day'
        assert not session.dirty
    engine.dispose()


def test_previous_day_late_discovery_is_included_but_old_unobserved_day_is_not(tmp_path):
    path = tmp_path / 'queue.sqlite3'
    initialize_sqlite(path)
    engine = create_engine(f'sqlite:///{path}')
    since = datetime(2026, 10, 26, 12, tzinfo=UTC)
    with Session(engine) as session:
        session.add(Source(id='official', city='belgrade', name='Official',
                           url='https://example.org', source_type='news', trust=1))
        session.flush()
        for key, observed in [('new', since + timedelta(minutes=1)),
                              ('old', since - timedelta(days=3))]:
            session.add(RawItem(source_id='official', external_id=key,
                url=f'https://example.org/{key}', title=f'Городское событие {key}', body=key,
                published_at=datetime(2026, 10, 23, tzinfo=ZONE), published_at_precision='day',
                ingested_at=observed))
        session.commit()
        result = select_candidates(session, city='belgrade', since=since,
                                   until=since + timedelta(hours=3), limit=2)
        assert [r['article_id'] for r in result['candidates']] == ['new']
        assert result['candidates'][0]['window_match'] == 'late_discovery'
    engine.dispose()


def test_v7_upgrade_preserves_rows_and_marks_legacy_precision_unknown(tmp_path):
    path = tmp_path / 'queue.sqlite3'
    initialize_sqlite(path)
    with sqlite3.connect(path) as db:
        db.execute('ALTER TABLE raw_item DROP COLUMN published_at_precision')
        db.execute('PRAGMA user_version=7')
        db.execute("INSERT INTO source(id,city,name,url,source_type,trust,ownership,enabled,"
                   "auto_publish,high_risk) VALUES('s','belgrade','s','https://example.org',"
                   "'news',1,'official',1,0,0)")
        db.execute("INSERT INTO raw_item(source_id,external_id,url,title,body) "
                   "VALUES('s','a','https://example.org/a','Заголовок','Текст')")
    backup = initialize_sqlite(path)
    assert backup == path.with_name(path.name + '.before-v8.backup')
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == SQLITE_SCHEMA_VERSION
        assert db.execute('SELECT published_at_precision FROM raw_item').fetchone() == ('unknown',)
    with sqlite3.connect(backup) as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 7
        assert db.execute('SELECT title FROM raw_item').fetchone() == ('Заголовок',)


def test_snapshot_roundtrip_keeps_publication_precision(tmp_path):
    path = tmp_path / 'queue.sqlite3'
    initialize_sqlite(path)
    engine = create_engine(f'sqlite:///{path}')
    with engine.begin() as db:
        db.exec_driver_sql("INSERT INTO source(id,city,name,url,source_type,trust,ownership,"
            "enabled,auto_publish,high_risk) VALUES('s','belgrade','s','https://example.org',"
            "'news',1,'official',1,0,0)")
        db.exec_driver_sql("INSERT INTO raw_item(source_id,external_id,url,title,body,"
            "published_at_precision) VALUES('s','a','https://example.org/a','Title','Body','day')")
    engine.dispose()
    manifest = create_snapshot(path, tmp_path / 'saved')
    manifest['drive_file_id'] = 'test_file_id'

    class Drive:
        def download(self, file_id):
            assert file_id == 'test_file_id'
            return (tmp_path / 'saved/editorial.sqlite3').read_bytes()

    target = tmp_path / 'restore'
    target.mkdir()
    restored_path = restored(Drive(), json.loads(json.dumps(manifest)), target)
    with sqlite3.connect(restored_path) as db:
        assert db.execute('SELECT published_at_precision FROM raw_item').fetchone() == ('day',)
