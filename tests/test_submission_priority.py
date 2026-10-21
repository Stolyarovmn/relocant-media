from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_venue_runtime import NOW, URL
from test_venue_runtime import runtime as _runtime

from relocant_media.collectors.venue_feed import VenueFeedCollector
from relocant_media.ops import server_cycle
from relocant_media.ops.candidates_cli import select_candidates
from relocant_media.services.submission_queue import pending_submissions, priority_raw_ids
from relocant_media.storage.models import RawItem, Submission

runtime = _runtime


@pytest.mark.asyncio
async def test_private_text_never_becomes_evidence_and_linked_article_is_first(runtime):
    engine, venue, _, _, _ = runtime
    with Session(engine) as session:
        session.add(Submission(kind='news_tip', status='new', payload=dict(
            city='belgrade', text='Частный телефон +38155555. Источник: '+URL)))
        session.add(Submission(kind='ad_request', status='new', payload=dict(
            city='belgrade', text='Реклама '+URL)))
        session.commit()
    stats = await server_cycle.collect_sources(engine)
    assert stats['priority_submissions'] == 1
    with Session(engine) as session:
        item = session.scalar(select(RawItem))
        assert '38155555' not in item.body
        ad = session.scalar(select(Submission).where(Submission.kind == 'ad_request'))
        assert ad.status == 'new'
        ids = priority_raw_ids(session)
        assert ids == {item.id}
        session.add(RawItem(source_id=venue.id, external_id='newer', url='https://museum.example/new/',
            title='Совсем другое новое событие', body='Другая тема', published_at=NOW))
        session.commit()
        manifest = select_candidates(session, city='belgrade', since=NOW-timedelta(days=1),
            until=NOW+timedelta(hours=1), limit=1, newest_first=True, priority_raw_ids=ids)
        assert manifest['candidates'][0]['raw_item_id'] == item.id
        assert '38155555' not in str(manifest)


@pytest.mark.asyncio
async def test_private_fetch_budget_keeps_unattempted_tips_new(runtime):
    engine, _, _, _, _ = runtime
    with Session(engine) as session:
        for index in range(20):
            session.add(Submission(kind='news_tip',status='new',payload=dict(
                city='belgrade',text=f'https://museum.example/unknown-{index}/')))
        session.commit()
    stats = await server_cycle.collect_sources(engine)
    assert stats['private_link_fetches'] == 10
    with Session(engine) as session:
        remaining = list(session.scalars(select(Submission).where(Submission.status == 'new')))
        assert len(remaining) == 10
        assert all(row.status == 'new' for row in pending_submissions(session, limit=10))


@pytest.mark.asyncio
async def test_malformed_private_url_does_not_break_public_collection(runtime):
    engine, _, _, _, _ = runtime
    with Session(engine) as session:
        session.add(Submission(kind='news_tip',status='new',payload=dict(
            city='belgrade',text='Ссылка https://[invalid')))
        session.commit()
    stats = await server_cycle.collect_sources(engine)
    assert stats['private_link_fetches'] == 0 and stats['venue_items'] == 1
    with Session(engine) as session:
        assert session.scalar(select(Submission)).status == 'waiting_primary'


@pytest.mark.asyncio
async def test_later_successful_feed_resolves_tip_after_transient_direct_fetch_failure(
    runtime, monkeypatch,
):
    engine, _, _, _, _ = runtime
    async def unavailable(*args, **kwargs):
        raise httpx.ConnectError('temporary failure')
    monkeypatch.setattr(VenueFeedCollector, 'collect_url', unavailable)
    with Session(engine) as session:
        session.add(Submission(kind='news_tip',status='new',payload=dict(city='belgrade',text=URL)))
        session.commit()
    stats = await server_cycle.collect_sources(engine)
    assert stats['priority_submissions'] == 1
    with Session(engine) as session:
        assert session.scalar(select(Submission)).status == 'source_linked'
