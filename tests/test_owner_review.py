import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from test_editorial_integration import prepare_service
from test_editorial_package import bind_context, package_payload, parse

from relocant_media.ai.models import EvidenceDocument, RussianDraft
from relocant_media.ai.package import EditorialPackage, canonical_json, draft_sha256
from relocant_media.domain.dedup import event_key
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID, deliver_reviewed
from relocant_media.services.editorial import DeliveryBlockedError, _candidate, _snapshot_member
from relocant_media.services.owner_review import (
    OWNER_REVIEW_KIND,
    deliver_owner_approved,
    queue_owner_reviews,
    record_owner_callback,
    record_owner_reply,
    replace_pending_package,
    revise_package,
)
from relocant_media.services.owner_review_prepare import prepare_high_risk_holds
from relocant_media.services.package_import import import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source, Submission
from relocant_media.storage.sqlite_schema import initialize_sqlite

NOW = datetime(2026, 10, 23, 8, tzinfo=UTC)
OWNER = 9001
OWNER_CHAT = 9001


def _package_hash(package: EditorialPackage) -> str:
    return hashlib.sha256(canonical_json(package.model_dump(mode="json")).encode()).hexdigest()


def _stored_package(run: EditorialRun) -> EditorialPackage:
    return EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))


def _live(package: EditorialPackage) -> tuple[EvidenceDocument, ...]:
    assert package.event is not None
    return tuple(
        EvidenceDocument.model_validate(
            document.model_dump(include=set(EvidenceDocument.model_fields))
        )
        for document in package.event.documents
    )


class ReviewProvider:
    def __init__(self, revision: RussianDraft | None = None) -> None:
        self.revision = revision
        self.calls: list[str] = []

    def generate_json(self, *, task, system_instruction, prompt, response_schema):
        del system_instruction, prompt, response_schema
        self.calls.append(task)
        if task == "owner_revision":
            assert self.revision is not None
            return self.revision.model_dump_json()
        if task == "independent_facts":
            return json.dumps({
                "verdict": "approve",
                "score": 95,
                "flags": [],
                "reason": "Факты подтверждены точным источником.",
            })
        if task == "independent_utility":
            return json.dumps({
                "verdict": "approve",
                "score": 100,
                "flags": [],
                "reason": "Материал полезен жителям Белграда.",
                "rubric": {
                    "local_relevance": 4,
                    "practical_value": 4,
                    "timeliness": 4,
                    "clarity": 4,
                },
            })
        raise AssertionError(f"unexpected AI task: {task}")


class ReviewBot:
    def __init__(self, message_id: int = 501) -> None:
        self.message_id = message_id
        self.calls: list[tuple[str, dict]] = []

    async def _call(self, method: str, payload: dict):
        self.calls.append((method, payload))
        if method == "sendMessage":
            return {"message_id": self.message_id}
        raise AssertionError(method)


class Publisher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def publish(self, text: str) -> int:
        self.calls.append(text)
        return 777


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "public")
    path = tmp_path / "owner-review.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    yield engine
    engine.dispose()


def _store_reviewed_politics(engine) -> int:
    with Session(engine) as session:
        _, _, raw_id = prepare_service(session)
        item = session.get(RawItem, raw_id)
        source = session.get(Source, item.source_id)
        assert item is not None and source is not None
        item.published_at = NOW
        session.commit()
        payload = package_payload()
        payload.update(
            version=2,
            policy_version="subscription-editorial-v2",
            raw_item_id=raw_id,
        )
        payload["outcome"]["ranking"]["category"] = "politics"
        payload["evidence"]["published_at"] = NOW.isoformat()
        payload["event"] = {
            "event_key": event_key(_candidate(item)),
            "editorial_rule_version": "belgrade-editorial-v2",
            "documents": [_snapshot_member(item, source)],
        }
        draft = parse({**payload, "reviews": payload["reviews"]}).outcome.draft
        for review in payload["reviews"]:
            review["reviewed_at"] = NOW.isoformat()
            review["draft_sha256"] = draft_sha256(draft)
        bind_context(payload)
        run = import_package(session, parse(payload), now=NOW)
        post = session.get(Post, run.post_id)
        assert post is not None and post.status == "human_review"
        return raw_id


def _owner_review_row(session: Session, raw_id: int, *, message_id: int = 501) -> Submission:
    run = session.get(EditorialRun, raw_id)
    assert run is not None
    package = _stored_package(run)
    row = Submission(
        kind=OWNER_REVIEW_KIND,
        telegram_user_id=OWNER,
        telegram_message_id=message_id,
        status="pending",
        payload={
            "city": "belgrade",
            "raw_item_id": raw_id,
            "post_id": run.post_id,
            "owner_chat_id": OWNER_CHAT,
            "package_sha256": _package_hash(package),
            "draft_sha256": draft_sha256(package.outcome.draft),
            "category": "politics",
            "version": 1,
        },
    )
    session.add(row)
    session.commit()
    return row


def test_high_risk_policy_hold_gets_two_reviews_but_stays_human_review(engine):
    with Session(engine) as session:
        service, _, raw_id = prepare_service(session, category="politics")
        run = service.prepare(raw_id)
        assert run.outcome["status"] == "human_review"
        assert run.outcome["reason"] == "policy_requires_human_review"
        assert "_package" not in run.outcome

    provider = ReviewProvider()
    result = prepare_high_risk_holds(engine, provider, _live)

    assert result == {"prepared": 1, "blocked": 0}
    assert provider.calls == ["independent_facts", "independent_utility"]
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        post = session.get(Post, run.post_id)
        package = _stored_package(run)
        assert run.outcome["status"] == "human_review"
        assert run.outcome["reason"] == "package_requires_review"
        assert post.status == "human_review"
        assert {review.role for review in package.reviews} == {"facts", "utility"}
        assert all(review.verdict == "approve" for review in package.reviews)


@pytest.mark.asyncio
async def test_owner_queue_has_publish_reject_buttons_and_is_idempotent(engine):
    raw_id = _store_reviewed_politics(engine)
    bot = ReviewBot()

    first = await queue_owner_reviews(
        engine,
        bot,
        owner_user_id=OWNER,
        owner_chat_id=OWNER_CHAT,
        enabled=True,
    )
    second = await queue_owner_reviews(
        engine,
        bot,
        owner_user_id=OWNER,
        owner_chat_id=OWNER_CHAT,
        enabled=True,
    )

    assert first == {"status": "ready", "queued": 1, "uncertain": 0}
    assert second == {"status": "ready", "queued": 0, "uncertain": 0}
    sends = [payload for method, payload in bot.calls if method == "sendMessage"]
    assert len(sends) == 1
    sent = sends[0]
    labels = [button["text"] for button in sent["reply_markup"]["inline_keyboard"][0]]
    assert labels == ["✅ Опубликовать", "❌ Отклонить"]
    assert "Источник" in sent["text"]
    assert "https://www.beograd.rs/example.html" not in sent["text"]
    assert sent["link_preview_options"]["url"] == "https://www.beograd.rs/example.html"
    with Session(engine) as session:
        review = session.scalar(select(Submission).where(Submission.kind == OWNER_REVIEW_KIND))
        assert review is not None and review.status == "pending"
        assert review.telegram_message_id == 501
        assert review.payload["raw_item_id"] == raw_id


def test_owner_callback_requires_exact_user_chat_message_and_pending_version(engine):
    raw_id = _store_reviewed_politics(engine)
    with Session(engine) as session:
        review = _owner_review_row(session, raw_id)
        callback = {
            "from": {"id": OWNER, "is_bot": False},
            "data": f"or:publish:{review.id}",
            "message": {"message_id": 501, "chat": {"id": OWNER_CHAT}},
        }
        wrong = {**callback, "from": {"id": OWNER + 1, "is_bot": False}}
        assert record_owner_callback(
            session, wrong, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT
        ) is None
        assert session.get(Submission, review.id).status == "pending"
        result = record_owner_callback(
            session, callback, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT
        )
        assert result is not None and result.accepted
        session.commit()
        assert session.get(Submission, review.id).status == "approved"
        stale = record_owner_callback(
            session, callback, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT
        )
        assert stale is not None and not stale.accepted


def test_owner_reply_requests_revision_only_for_exact_pending_review(engine):
    raw_id = _store_reviewed_politics(engine)
    with Session(engine) as session:
        review = _owner_review_row(session, raw_id)
        message = {
            "from": {"id": OWNER, "is_bot": False},
            "chat": {"id": OWNER_CHAT, "type": "private"},
            "text": "Сделай заголовок короче, факты не меняй.",
            "reply_to_message": {"message_id": 501},
        }
        result = record_owner_reply(
            session, message, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT
        )
        assert result is not None and result.accepted
        session.commit()
        stored = session.get(Submission, review.id)
        assert stored.status == "revision_requested"
        assert stored.payload["owner_comment"].startswith("Сделай заголовок")
        stale = record_owner_reply(
            session, message, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT
        )
        assert stale is not None and not stale.accepted


def test_owner_revision_discards_old_reviews_and_creates_new_exact_version(engine):
    raw_id = _store_reviewed_politics(engine)
    revised_draft = RussianDraft(
        title="Отключение воды на Звездаре",
        body="Часть Звездары останется без воды с 08:00 до 18:00.",
        claim_ids=("c1",),
    )
    provider = ReviewProvider(revised_draft)
    with Session(engine) as session:
        review = _owner_review_row(session, raw_id)
        review.status = "revision_requested"
        review.payload = {**review.payload, "owner_comment": "Сократи заголовок."}
        run = session.get(EditorialRun, raw_id)
        package = _stored_package(run)
        session.commit()

    revised = revise_package(
        package,
        provider,
        _live,
        "Сократи заголовок.",
    )
    assert provider.calls == ["owner_revision", "independent_facts", "independent_utility"]
    assert revised.outcome.draft.title == "Отключение воды на Звездаре"
    assert all(review.reviewer_id.startswith("server-") for review in revised.reviews)

    with Session(engine) as session:
        review = session.scalar(select(Submission).where(Submission.kind == OWNER_REVIEW_KIND))
        replace_pending_package(session, review, revised)
        run = session.get(EditorialRun, raw_id)
        post = session.get(Post, run.post_id)
        assert review.status == "superseded"
        assert post.title == "Отключение воды на Звездаре"
        assert run.outcome["_package_sha256"] == _package_hash(revised)
        assert review.payload["successor_package_sha256"] == _package_hash(revised)


@pytest.mark.asyncio
async def test_owner_publish_is_separate_at_most_once_path(engine):
    raw_id = _store_reviewed_politics(engine)
    publisher = Publisher()
    with Session(engine) as session:
        review = _owner_review_row(session, raw_id)
        review.status = "approved"
        run = session.get(EditorialRun, raw_id)
        package = _stored_package(run)
        session.commit()

        with pytest.raises(DeliveryBlockedError):
            await deliver_reviewed(
                session,
                raw_id,
                publisher,
                target_channel_id=APPROVED_CHANNEL_ID,
                enabled=True,
            )
        result = await deliver_owner_approved(
            session,
            review,
            publisher,
            live_documents=_live(package),
            target_channel_id=APPROVED_CHANNEL_ID,
            enabled=True,
        )
        assert result.message_id == 777
        assert len(publisher.calls) == 1
        session.refresh(run)
        session.refresh(review)
        assert run.delivery_status == "sent"
        assert review.status == "published"
        post = session.get(Post, run.post_id)
        assert post.telegram_message_id == 777
        with pytest.raises(DeliveryBlockedError):
            await deliver_owner_approved(
                session,
                review,
                publisher,
                live_documents=_live(package),
                target_channel_id=APPROVED_CHANNEL_ID,
                enabled=True,
            )
        assert len(publisher.calls) == 1


def test_owner_review_rows_never_enter_normal_submission_queue(engine):
    raw_id = _store_reviewed_politics(engine)
    with Session(engine) as session:
        _owner_review_row(session, raw_id)
        from relocant_media.services.submission_queue import batch_submissions

        assert batch_submissions(session) == []
        assert session.scalar(
            select(func.count()).select_from(Submission).where(
                Submission.kind == OWNER_REVIEW_KIND
            )
        ) == 1
