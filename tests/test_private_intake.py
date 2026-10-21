import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from relocant_media.services import private_intake
from relocant_media.services.private_intake import receive_private_tips
from relocant_media.storage.models import Post, RawItem, Submission, TelegramUpdateReceipt
from relocant_media.storage.sqlite_schema import initialize_sqlite


@pytest.fixture
def engine(tmp_path):
    path = tmp_path / 'intake.sqlite3'
    initialize_sqlite(path)
    engine = create_engine(f'sqlite:///{path}')
    yield engine
    engine.dispose()


def incoming(uid=101, parent=-1003967014133):
    return dict(update_id=uid, message=dict(message_id=7,
        chat=dict(id=-2000000000011, is_direct_messages=True),
        direct_messages_topic=dict(topic_id=23),
        **{'from': dict(id=23, is_bot=False)},
        text='Закрытый текст автора; не пересылать.'))


class Bot:
    def __init__(self, updates=None, rights=True, webhook='', parent=-1003967014133):
        self.updates = updates or []
        self.rights, self.webhook, self.parent = rights, webhook, parent
        self.calls = []

    async def _call(self, method, params):
        self.calls.append((method, params))
        # This receiver has no outgoing action, even on successful acceptance.
        assert method in {'getWebhookInfo', 'getMe', 'getChatMember', 'getUpdates', 'getChat'}
        return {'getWebhookInfo': dict(url=self.webhook), 'getMe': dict(id=1),
            'getChatMember': dict(can_manage_direct_messages=self.rights),
            'getChat': dict(parent_chat=dict(id=self.parent)), 'getUpdates': self.updates}[method]


@pytest.mark.asyncio
async def test_disabled_intake_has_no_network_side_effect(engine):
    bot = Bot([incoming()])
    assert (await receive_private_tips(engine, bot, enabled=False))['status'] == 'disabled'
    assert bot.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(('rights', 'webhook', 'status'), [
    (False, '', 'blocked_direct_message_access'),
    (True, 'https://existing.example/webhook', 'blocked_existing_webhook'),
])
async def test_access_or_existing_webhook_blocks_before_consuming(engine, rights, webhook, status):
    bot = Bot([incoming()], rights=rights, webhook=webhook)
    assert (await receive_private_tips(engine, bot, enabled=True))['status'] == status
    assert all(name != 'getUpdates' for name, _ in bot.calls)


@pytest.mark.asyncio
async def test_tip_stays_private_and_receipt_is_idempotent(engine):
    bot = Bot([incoming()])
    first = await receive_private_tips(engine, bot, enabled=True)
    second = await receive_private_tips(engine, bot, enabled=True)
    assert first['received'] == 1 and second['received'] == 0
    assert 'Закрытый текст' not in str(first)
    assert [p for name, p in bot.calls if name == 'getUpdates'][-1]['offset'] == 102
    with Session(engine) as session:
        submission = session.scalar(select(Submission))
        assert submission.payload['city'] == 'belgrade' and submission.status == 'new'
        assert session.scalar(select(func.count()).select_from(RawItem)) == 0
        assert session.scalar(select(func.count()).select_from(Post)) == 0


@pytest.mark.asyncio
async def test_other_channel_direct_messages_not_saved(engine):
    bot = Bot([incoming()], parent=-1001234567890)
    assert (await receive_private_tips(engine, bot, enabled=True))['received'] == 0
    with Session(engine) as session:
        assert session.scalar(select(Submission)) is None


@pytest.mark.asyncio
async def test_unfinished_old_receipt_is_not_skipped_by_new_offset(engine):
    with Session(engine) as session:
        session.add(TelegramUpdateReceipt(update_id=100, kind='message',status='failed'))
        session.commit()
    bot = Bot([incoming()])
    result = await receive_private_tips(engine, bot, enabled=True)
    assert result['status'] == 'blocked_unresolved_receipts'
    assert all(name != 'getUpdates' for name, _ in bot.calls)


@pytest.mark.asyncio
async def test_storage_failure_does_not_acknowledge_update(engine, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('storage failure')
    monkeypatch.setattr(private_intake, 'save_channel_tip', fail)
    bot = Bot([incoming()])
    with pytest.raises(RuntimeError):
        await receive_private_tips(engine, bot, enabled=True)
    with Session(engine) as session:
        assert session.get(TelegramUpdateReceipt, 101) is None
    assert len([1 for name, _ in bot.calls if name == 'getUpdates']) == 1
