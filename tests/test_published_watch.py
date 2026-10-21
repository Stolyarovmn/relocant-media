from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ingestion.persistence import sync_source
from relocant_media.ops.candidates_cli import select_candidates
from relocant_media.services.published_watch import (
    evidence_changed,
    published_sources,
    recent_source_load,
)
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import EditorialRun, Post, RawItem
from relocant_media.storage.sqlite_schema import initialize_sqlite


def test_sent_sources_monitored_without_rewriting_published_text_and_load_breaks_ties(tmp_path):
    path = tmp_path/'watch.sqlite3'
    initialize_sqlite(path)
    engine = create_engine(f'sqlite:///{path}')
    now = datetime(2026, 10, 21, 6, tzinfo=UTC)
    with Session(engine) as s:
        for source_id in ('city_beograd_news', 'cukarica_news'):
            sync_source(s, 'belgrade', load_source_file(
                Path('cities/belgrade/sources')/(source_id+'.yaml')))
        first = RawItem(source_id='city_beograd_news', external_id='sent',url='https://a.test/1',
            title='Первый материал',body='Подтверждено прежде',published_at=now)
        s.add(first)
        s.flush()
        post = Post(city='belgrade', category='events',status='sent',title='Опубликованный текст',
            body='Не изменять',confidence=Decimal('1'),published_at=now,telegram_message_id=20)
        s.add(post)
        s.flush()
        s.add(EditorialRun(raw_item_id=first.id,post_id=post.id,evidence={},outcome={},
                           delivery_status='sent'))
        for index, sid in enumerate(('city_beograd_news', 'cukarica_news')):
            s.add(RawItem(source_id=sid, external_id='next',url=f'https://{index}.test/news',
                title=('Ремонт на северном берегу', 'Выставка науки открывается в музее')[index],
                body=f'Разные факты {index}',
                published_at=now))
        s.commit()
        assert published_sources(s, now=now) == [first]
        assert evidence_changed(first, SimpleNamespace(url=first.url,title=first.title,
            body='Отмена события',published_at=now))
        load = recent_source_load(s,now=now)
        assert load == {'city_beograd_news':1}
        manifest = select_candidates(s,city='belgrade',since=now.replace(hour=0),
            until=now.replace(hour=7),limit=1,newest_first=True,recent_source_counts=load)
        assert manifest['candidates'][0]['source_id']=='cukarica_news'
        assert post.body == 'Не изменять' and post.telegram_message_id == 20
    engine.dispose()
