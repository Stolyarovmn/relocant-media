from contextlib import nullcontext
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session
from test_primary_cycle import NOW as WINDOW_NOW
from test_primary_cycle import ingest_ok
from test_primary_cycle import queue as _queue
from test_private_intake import Bot, incoming
from test_private_intake import engine as _engine
from test_venue_runtime import NOW, URL
from test_venue_runtime import runtime as _runtime

from relocant_media.ops import server_cycle
from relocant_media.ops.candidates_cli import select_candidates
from relocant_media.ops.intake_batch import collect_batch
from relocant_media.ops.primary_cycle import begin_window, run_cycle
from relocant_media.services.private_intake import receive_private_tips
from relocant_media.services.submission_queue import batch_submissions
from relocant_media.storage.models import EditorialRun, RawItem, Submission, TelegramUpdateReceipt

engine, runtime, queue, editorial_session = _engine, _runtime, _queue, _editorial_session


@pytest.mark.asyncio
async def test_busy_storage_does_not_ack_or_store_then_replays_once(engine):
    bot = Bot([incoming()])
    first = await receive_private_tips(engine,bot,enabled=True,poll_timeout=1,
                                       store_guard=lambda: nullcontext(False))
    assert first['status'] == 'busy'
    with Session(engine) as s:
        assert s.scalar(select(Submission)) is None
        assert s.scalar(select(TelegramUpdateReceipt)) is None
    second = await receive_private_tips(engine,bot,enabled=True,poll_timeout=1,
                                        store_guard=lambda: nullcontext(True))
    assert second['received'] == 1
    assert all(params['timeout'] == 1 for method,params in bot.calls if method == 'getUpdates')
    assert all('offset' not in params for method,params in bot.calls if method == 'getUpdates')


@pytest.mark.asyncio
async def test_ten_messages_form_one_batch_and_one_event_candidate(runtime):
    engine, _, _, _, requests = runtime
    updates = []
    for i in range(10):
        value = incoming(uid=101+i)
        value['message']['message_id'] = 7+i
        value['message']['from']['id'] = 23+i
        value['message']['text'] = 'Предлагаю событие '+URL
        updates.append(value)
    calls = []
    result = await collect_batch(engine,Bot(updates),enabled=True,
        store_guard=lambda: nullcontext(True),state={},
        run_pipeline=lambda: calls.append('one') or 0)
    assert result['intake']['received'] == 10 and calls == ['one']
    assert result['pipeline_started'] is True
    stats = await server_cycle.collect_sources(engine,submissions_only=True)
    assert stats['private_link_fetches'] == 1 and stats['raw_items_written'] == 1
    assert not any('beograd.rs' in url for url in requests)
    assert stats['channel_leads'] == 0 and stats['venue_items'] == 0
    with Session(engine) as s:
        rows = list(s.scalars(select(Submission)))
        assert len(rows) == 10 and all(r.status == 'source_linked' for r in rows)
        manifest = select_candidates(s,city='belgrade',since=NOW-timedelta(days=1),
            until=NOW,limit=20,allowed_raw_ids=set(stats['submission_raw_ids']))
        assert manifest['candidate_count'] == 1
        raw_id = manifest['candidates'][0]['raw_item_id']
        s.add(EditorialRun(raw_item_id=raw_id,outcome={'status':'skipped'},evidence={}))
        s.commit()
    again = await collect_batch(engine,Bot(updates),enabled=True,
        store_guard=lambda: nullcontext(True),state={},
        run_pipeline=lambda: calls.append('bad') or 0)
    assert again['intake']['received'] == 0 and again['pipeline_started'] is False
    assert calls == ['one']


@pytest.mark.asyncio
async def test_missing_facts_do_not_collect_public_sites_or_create_evidence(runtime):
    engine, _, _, _, requests = runtime
    with Session(engine) as s:
        s.add(Submission(kind='news_tip',status='new',payload=dict(
            city='belgrade',text='Слышал слух, источника нет')))
        s.commit()
    stats = await server_cycle.collect_sources(engine,submissions_only=True)
    assert stats['submission_raw_ids'] == [] and requests == []
    with Session(engine) as s:
        assert s.scalar(select(RawItem)) is None
        assert s.scalar(select(Submission)).payload['triage_reason'] == 'missing_primary'


@pytest.mark.asyncio
async def test_ads_do_not_trigger_news_pipeline_and_restart_recovers_unsent_work(engine):
    calls = []
    with Session(engine) as s:
        s.add(Submission(kind='ad_request',status='new',payload=dict(city='belgrade',text='реклама')))
        s.commit()
    args = dict(enabled=True,store_guard=lambda: nullcontext(True),state={},
                run_pipeline=lambda: calls.append('run') or 0)
    assert (await collect_batch(engine,Bot(),**args))['pipeline_started'] is False
    with Session(engine) as s:
        s.add(Submission(kind='news_tip',status='new',payload=dict(city='belgrade',text='новость')))
        s.commit()
    assert (await collect_batch(engine,Bot(),**args))['pipeline_started'] is True
    assert calls == ['run']


@pytest.mark.asyncio
async def test_unchanged_failed_batch_has_durable_retry_budget(engine):
    with Session(engine) as s:
        s.add(Submission(kind='news_tip',status='new',payload=dict(city='belgrade',text='тема')))
        s.commit()
    state, checkpoints, calls = {}, [], []
    for _ in range(4):
        result = await collect_batch(engine,Bot(),enabled=True,
            store_guard=lambda: nullcontext(True),state=state,
            checkpoint=lambda: checkpoints.append(state.copy()),
            run_pipeline=lambda: calls.append('failed') or 1)
    assert len(calls) == 3 and len(checkpoints) == 3
    assert checkpoints[-1]['attempts'] == 3
    assert result['status'] == 'blocked_retry_budget' and not result['pipeline_started']


def test_scoped_primary_does_not_touch_normal_checkpoint_or_unrelated_items(queue,tmp_path):
    engine, process = queue
    with Session(engine) as s:
        first = s.scalar(select(RawItem.id).order_by(RawItem.id))
        normal = begin_window(s,city='belgrade',now=WINDOW_NOW)
        normal_id = normal.id
    calls = []
    report = run_cycle(engine,city='belgrade',output=tmp_path/'scoped',now=WINDOW_NOW,
        ingest=ingest_ok,process=lambda rid: calls.append(rid) or process(rid),
        allowed_items=lambda: {first},submissions_only=True)
    assert report['status'] == 'PRIMARY_OK' and calls == [first]
    with Session(engine) as s:
        from relocant_media.scheduler.models import SchedulerRun
        assert s.get(SchedulerRun,normal_id).status == 'running'
        keys = list(s.scalars(select(SchedulerRun.task_key)))
        assert 'editorial-primary:belgrade:submissions' in keys


@pytest.mark.asyncio
async def test_empty_delivery_scope_never_sends_unrelated_ready_posts(engine,monkeypatch):
    class NoNetwork:
        def __init__(self,**kwargs):
            raise AssertionError('No delivery request without scoped evidence')
    # Disabled delivery already has a fast exit. An empty enabled scope must as well.
    monkeypatch.setattr(server_cycle.httpx,'AsyncClient',NoNetwork)
    assert await server_cycle.publish_pending(engine,enabled=True,only_raw_ids=set()) == []


def test_already_reviewed_linked_tips_are_not_new_work(engine):
    with Session(engine) as s:
        s.add(Submission(kind='news_tip',status='source_linked',payload=dict(
            city='belgrade',raw_item_ids=[])))
        s.commit()
        assert batch_submissions(s) == []


@pytest.mark.asyncio
async def test_different_raw_id_of_reviewed_event_does_not_retrigger_batch(runtime):
    engine, _, _, _, _ = runtime
    await server_cycle.collect_sources(engine)
    with Session(engine) as s:
        original = s.scalar(select(RawItem))
        s.add(EditorialRun(raw_item_id=original.id,outcome={'status':'skipped'},evidence={}))
        clone = RawItem(source_id=original.source_id,external_id='another-url',
            url='https://museum.example/another/',title=original.title,body=original.body,
            published_at=original.published_at,canonical_url=original.url)
        s.add(clone)
        s.flush()
        s.add(Submission(kind='news_tip',status='source_linked',payload=dict(
            city='belgrade',raw_item_ids=[clone.id])))
        s.commit()
        assert batch_submissions(s) == []
