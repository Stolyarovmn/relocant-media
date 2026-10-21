from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session

from relocant_media.services.submission_feedback import (
    reply_to_submissions,
    resolve_submission,
    save_requested_followup,
)
from relocant_media.storage.models import Post, RawItem, Submission
from relocant_media.storage.sqlite_schema import initialize_sqlite

editorial_session = _editorial_session


@pytest.fixture
def engine(tmp_path):
    path = tmp_path / 'feedback.sqlite3'
    initialize_sqlite(path)
    engine = create_engine(f'sqlite:///{path}')
    yield engine
    engine.dispose()


def seed(engine, text='Все музеи будут бесплатными, знакомый сказал.', ids=None,
         age=timedelta(), **payload):
    now = datetime.now(UTC)
    with Session(engine) as s:
        row = Submission(kind='news_tip', telegram_user_id=23, telegram_message_id=7,
            created_at=now-age, payload=dict(city='belgrade',chat_id=23,text=text,**payload))
        s.add(row)
        s.flush()
        resolve_submission(row, ids or [], now=now)
        s.commit()
        return row.id


class Bot:
    def __init__(self, fail=False, parent=-1003967014133):
        self.calls, self.fail, self.parent = [], fail, parent

    async def _call(self, method, params):
        self.calls.append((method, params))
        assert method in {'sendMessage', 'getChat'}
        if method == 'getChat':
            return dict(is_direct_messages=True, parent_chat=dict(id=self.parent))
        if self.fail:
            raise httpx.ReadTimeout('may have been delivered')
        return dict(message_id=99)


@pytest.mark.asyncio
async def test_fiction_waits_for_evidence_and_gets_one_private_request(engine):
    sid = seed(engine)
    bot = Bot()
    assert (await reply_to_submissions(engine,bot,enabled=True))['sent'] == 1
    assert (await reply_to_submissions(engine,bot,enabled=True))['sent'] == 0
    method, request = bot.calls[0]
    assert method == 'sendMessage' and request['chat_id'] == 23
    assert request['reply_parameters'] == {'message_id': 7}
    assert request['allow_paid_broadcast'] is False
    assert 'недостаточно' in request['text'] and 'Все музеи' not in request['text']
    with Session(engine) as s:
        row = s.get(Submission,sid)
        assert row.status == 'waiting_primary'
        assert row.payload['feedback']['missing_primary']['status'] == 'sent'
        assert s.scalar(select(Post)) is None and s.scalar(select(RawItem)) is None


@pytest.mark.asyncio
async def test_real_source_is_not_claimed_as_published_or_all_claims_verified(engine):
    seed(engine, ids=[123])
    bot = Bot()
    await reply_to_submissions(engine,bot,enabled=True)
    assert 'не подтверждение всех утверждений' in bot.calls[0][1]['text']
    assert 'не обещание публикации' in bot.calls[0][1]['text']


@pytest.mark.asyncio
async def test_witness_requires_location_time_direction_and_no_automatic_publication(engine):
    seed(engine,text='На перекрёстке авария, затор, проехать нельзя')
    bot = Bot()
    await reply_to_submissions(engine,bot,enabled=True)
    text = bot.calls[0][1]['text']
    assert 'направление' in text and 'время наблюдения' in text
    assert 'неподтверждённое' in text and 'не снимайте за рулём' in text
    assert 'мгновенную проверку не гарантируем' in text


@pytest.mark.parametrize(('text','age','expected'), [
    ('авария на перекрёстке', timedelta(minutes=61), 'expired'),
    ('авария на перекрёстке', timedelta(minutes=59), 'waiting_primary'),
    ('музей откроется', timedelta(hours=49), 'expired'),
])
def test_unconfirmed_reports_expire_without_model_calls(engine,text,age,expected):
    sid = seed(engine,text=text,age=age)
    with Session(engine) as s:
        assert s.get(Submission,sid).status == expected


@pytest.mark.asyncio
async def test_uncertain_reply_is_durable_and_never_resent(engine):
    sid = seed(engine)
    bot = Bot(fail=True)
    assert (await reply_to_submissions(engine,bot,enabled=True))['uncertain'] == 1
    retry = await reply_to_submissions(engine,bot,enabled=True)
    assert retry['sent'] == 0 and retry['status'] == 'requires_review'
    assert retry['unresolved'] == 1
    assert len(bot.calls) == 1
    with Session(engine) as s:
        assert s.get(Submission,sid).payload['feedback']['missing_primary']['status'] == 'uncertain'


@pytest.mark.asyncio
async def test_native_reply_cannot_escape_channel_private_topic(engine):
    sid = seed(engine)
    with Session(engine) as s:
        row = s.get(Submission,sid)
        row.payload = {**row.payload, 'chat_id': -2073967014133,
            'origin': 'channel_direct_messages','channel_id': -1003967014133,'topic_id': 23}
        s.commit()
    bot = Bot()
    assert (await reply_to_submissions(engine,bot,enabled=True))['sent'] == 1
    assert bot.calls[-1][1]['direct_messages_topic_id'] == 23
    assert bot.calls[-1][1]['chat_id'] == -2073967014133


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['public_channel','wrong_parent','other_user'])
async def test_wrong_destinations_block_without_sending(engine,mode):
    sid = seed(engine)
    with Session(engine) as s:
        row = s.get(Submission,sid)
        if mode == 'public_channel':
            row.payload = {**row.payload,'chat_id': -1003967014133}
        elif mode == 'other_user':
            row.payload = {**row.payload,'chat_id': 24}
        else:
            row.payload = {**row.payload,'chat_id': -2073967014133,
                'origin':'channel_direct_messages','channel_id':-1003967014133,'topic_id':23}
        s.commit()
    bot = Bot(parent=-1001111111111)
    assert (await reply_to_submissions(engine,bot,enabled=True))['blocked'] == 1
    assert all(method != 'sendMessage' for method,_ in bot.calls)


@pytest.mark.asyncio
async def test_disabled_and_test_rows_never_send(engine):
    seed(engine)
    seed(engine,test_only=True)
    bot = Bot()
    assert (await reply_to_submissions(engine,bot,enabled=False))['sent'] == 0
    assert bot.calls == []
    assert (await reply_to_submissions(engine,bot,enabled=True))['sent'] == 1


@pytest.mark.asyncio
async def test_already_answered_rows_do_not_starve_new_requests(engine):
    for _ in range(21):
        seed(engine)
    bot = Bot()
    assert (await reply_to_submissions(engine,bot,enabled=True))['sent'] == 20
    assert (await reply_to_submissions(engine,bot,enabled=True))['sent'] == 1


@pytest.mark.asyncio
async def test_followup_is_bound_to_same_author_and_our_actual_reply(engine):
    sid = seed(engine)
    await reply_to_submissions(engine,Bot(),enabled=True)
    message = dict(message_id=8,chat=dict(id=23),text='Вот ссылка https://www.beograd.rs/test',
                   reply_to_message=dict(message_id=99), **{'from':dict(id=23)})
    with Session(engine) as s:
        assert not save_requested_followup(s,{**message,'from':dict(id=24)})
        assert save_requested_followup(s,message)
        s.commit()
        rows = list(s.scalars(select(Submission).order_by(Submission.id)))
        assert rows[0].id == sid and rows[0].status == 'superseded'
        assert rows[1].status == 'new' and rows[1].payload['followup_to'] == sid
        assert 'Все музеи' in rows[1].payload['text']
        assert 'https://www.beograd.rs/test' in rows[1].payload['text']


@pytest.mark.asyncio
async def test_feedback_json_selection_works_on_postgresql(editorial_session):
    s = editorial_session
    row = Submission(kind='news_tip',telegram_user_id=23,telegram_message_id=7,
        payload=dict(city='belgrade',chat_id=23,text='Без подтверждений'),
        created_at=datetime.now(UTC))
    s.add(row)
    s.flush()
    resolve_submission(row,[],now=datetime.now(UTC))
    s.commit()
    sid = row.id
    try:
        bot = Bot()
        assert (await reply_to_submissions(s.bind,bot,enabled=True,only_ids={sid}))['sent'] == 1
        assert (await reply_to_submissions(s.bind,bot,enabled=True,only_ids={sid}))['sent'] == 0
    finally:
        s.delete(s.get(Submission,sid))
        s.commit()
