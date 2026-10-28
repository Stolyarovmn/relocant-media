import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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
    process_owner_actions,
    queue_owner_reviews,
    record_owner_callback,
    record_owner_reply,
    replace_pending_package,
    revise_package,
)
from relocant_media.services.owner_review_prepare import (
    pending_owner_decision_holds,
    prepare_owner_decision_holds,
)
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
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW.replace(tzinfo=None) if tz is None else NOW.astimezone(tz)

    # Keep review creation and validation on the same clock as the fixed source dates.
    for module in (
        "relocant_media.ai.package",
        "relocant_media.ai.independent_reviews",
        "relocant_media.services.owner_review",
        "relocant_media.services.owner_review_prepare",
    ):
        monkeypatch.setattr(f"{module}.datetime", FixedDateTime)
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
        item = session.get(RawItem, raw_id)
        item.published_at = NOW
        session.commit()
        run = service.prepare(raw_id)
        assert run.outcome["status"] == "human_review"
        assert run.outcome["reason"] == "policy_requires_human_review"
        assert "_package" not in run.outcome

    provider = ReviewProvider()
    result = prepare_owner_decision_holds(engine, provider, _live, now=NOW)

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


def _undecided_event_hold(
    engine, *, reason="source_auto_publish_disabled", age=timedelta(0),
    ownership="official", category="events", guide=False,
):
    """A fresh, drafted item the editorial could not decide on its own."""
    with Session(engine) as session:
        service, _, raw_id = prepare_service(session, category=category)
        item = session.get(RawItem, raw_id)
        source = session.get(Source, item.source_id)
        item.published_at = None if guide else NOW - age
        if guide:
            # Evergreen guide: no publication date, valid from the day its text was first seen.
            source.source_type = "official_guide"
            item.ingested_at = NOW - age
        source.auto_publish = False
        source.ownership = ownership
        session.commit()
        run = service.prepare(raw_id)
        assert run.outcome["status"] == "human_review"
        assert run.outcome["reason"] in {
            "source_auto_publish_disabled", "policy_requires_human_review",
        }
        if reason != "source_auto_publish_disabled":
            run.outcome = {**run.outcome, "reason": reason}
            session.commit()
        return raw_id


@pytest.mark.asyncio
async def test_fresh_low_risk_hold_is_reviewed_and_offered_to_the_owner(engine):
    raw_id = _undecided_event_hold(engine)
    assert pending_owner_decision_holds(engine, now=NOW) == [raw_id]

    provider = ReviewProvider()
    assert prepare_owner_decision_holds(engine, provider, _live, now=NOW) == {
        "prepared": 1, "blocked": 0,
    }
    assert provider.calls == ["independent_facts", "independent_utility"]
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        assert run.outcome["status"] == "human_review"
        assert run.outcome["reason"] == "package_requires_review"
        assert session.get(Post, run.post_id).status == "human_review"

    bot = ReviewBot()
    queued = await queue_owner_reviews(
        engine, bot, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT, enabled=True,
    )
    assert queued == {"status": "ready", "queued": 1, "uncertain": 0}
    send = next(payload for method, payload in bot.calls if method == "sendMessage")
    assert _button_labels(send) == [
        "✅ В редакцию", "📝 В черновик", "✏️ Доработать", "⏭ Пропустить", "❌ Отклонить",
    ]


@pytest.mark.parametrize(
    "reason", ["publication_date_unknown", "source_changed_requires_review"],
)
def test_provenance_and_date_holds_never_become_owner_decisions(engine, reason):
    _undecided_event_hold(engine, reason=reason)
    provider = ReviewProvider()
    assert pending_owner_decision_holds(engine, now=NOW) == []
    assert prepare_owner_decision_holds(engine, provider, _live, now=NOW) == {
        "prepared": 0, "blocked": 0,
    }
    assert provider.calls == []


def test_stale_hold_is_not_offered_to_the_owner(engine):
    _undecided_event_hold(engine, age=timedelta(days=3))
    assert pending_owner_decision_holds(engine, now=NOW) == []


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
    assert _button_labels(sent) == [
        "✅ В редакцию", "📝 В черновик", "✏️ Доработать", "⏭ Пропустить", "❌ Отклонить",
    ]
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


def _button_labels(payload):
    return [button["text"] for row in payload["reply_markup"]["inline_keyboard"]
            for button in row]


def _press(session, review_id, action, *, message_id=501):
    return record_owner_callback(session, {
        "from": {"id": OWNER, "is_bot": False}, "data": f"or:{action}:{review_id}",
        "message": {"message_id": message_id, "chat": {"id": OWNER_CHAT}},
    }, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT)


@pytest.mark.parametrize(("action", "status"), [
    ("skip", "skipped"), ("draft", "scheduled"), ("reject", "rejected"), ("publish", "approved"),
])
def test_each_owner_button_moves_the_exact_version_to_its_own_pipeline(engine, action, status):
    raw_id = _store_reviewed_politics(engine)
    with Session(engine) as session:
        review = _owner_review_row(session, raw_id)
        result = _press(session, review.id, action)
        assert result is not None and result.accepted
        session.commit()
        assert session.get(Submission, review.id).status == status
        assert session.get(Submission, review.id).payload["owner_decision"] == action


def test_revise_button_asks_for_a_reply_and_changes_nothing(engine):
    raw_id = _store_reviewed_politics(engine)
    with Session(engine) as session:
        review = _owner_review_row(session, raw_id)
        result = _press(session, review.id, "revise")
        assert result is not None and result.handled and not result.accepted
        assert "ответ" in result.answer_text.lower()
        assert result.clear_chat_id is None  # the buttons stay for the next decision
        assert session.get(Submission, review.id).status == "pending"


@pytest.mark.asyncio
async def test_skipped_version_is_never_offered_again(engine):
    raw_id = _store_reviewed_politics(engine)
    with Session(engine) as session:
        review = _owner_review_row(session, raw_id)
        _press(session, review.id, "skip")
        session.commit()
    bot = ReviewBot()
    queued = await queue_owner_reviews(
        engine, bot, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT, enabled=True,
    )
    assert queued["queued"] == 0 and bot.calls == []


@pytest.mark.asyncio
async def test_scheduled_drafts_are_released_one_per_interval(engine):
    first = _store_reviewed_politics(engine)
    publisher = Publisher()

    async def run_actions():
        return await process_owner_actions(
            engine, ReviewBot(), owner_user_id=OWNER, owner_chat_id=OWNER_CHAT,
            provider=None, live_document_fetcher=_live, publisher=publisher,
            publish_enabled=True,
        )

    with Session(engine) as session:
        review = _owner_review_row(session, first)
        _press(session, review.id, "draft")
        # Another post went out ten minutes ago: the channel already has its news of this hour.
        session.add(Post(
            city="belgrade", category="events", status="ready", title="t", body="b",
            confidence=Decimal("0.9"), editorial_rule_version="v", telegram_message_id=5,
            published_at=NOW - timedelta(minutes=10),
        ))
        session.commit()
    assert (await run_actions())["published"] == 0
    assert publisher.calls == []
    with Session(engine) as session:
        assert session.scalar(select(Submission.status).where(
            Submission.kind == OWNER_REVIEW_KIND)) == "scheduled"
        recent = session.scalar(select(Post).where(Post.telegram_message_id == 5))
        recent.published_at = NOW - timedelta(minutes=70)
        session.commit()
    assert (await run_actions())["published"] == 1
    assert len(publisher.calls) == 1


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


@pytest.mark.asyncio
async def test_owner_approval_of_low_risk_hold_still_runs_every_delivery_check(engine):
    raw_id = _undecided_event_hold(engine)
    assert prepare_owner_decision_holds(engine, ReviewProvider(), _live, now=NOW)["prepared"] == 1
    await queue_owner_reviews(
        engine, ReviewBot(), owner_user_id=OWNER, owner_chat_id=OWNER_CHAT, enabled=True,
    )
    publisher = Publisher()
    with Session(engine) as session:
        review = session.scalar(select(Submission).where(Submission.kind == OWNER_REVIEW_KIND))
        review.status = "approved"
        run = session.get(EditorialRun, raw_id)
        package = _stored_package(run)
        session.commit()
        with pytest.raises(DeliveryBlockedError):  # the automatic path stays closed
            await deliver_reviewed(
                session, raw_id, publisher, target_channel_id=APPROVED_CHANNEL_ID, enabled=True,
            )
        changed = tuple(
            document.model_copy(update={"body": document.body + " Izmena izvora."})
            for document in _live(package)
        )
        with pytest.raises(DeliveryBlockedError):  # approval never skips the live-source check
            await deliver_owner_approved(
                session, review, publisher, live_documents=changed,
                target_channel_id=APPROVED_CHANNEL_ID, enabled=True,
            )
        assert publisher.calls == []
        result = await deliver_owner_approved(
            session, review, publisher, live_documents=_live(package),
            target_channel_id=APPROVED_CHANNEL_ID, enabled=True,
        )
        assert result.message_id == 777 and len(publisher.calls) == 1


@pytest.mark.asyncio
async def test_media_story_reaches_owner_cites_the_original_and_publishes_after_checks(engine):
    raw_id = _undecided_event_hold(engine, ownership="media")
    assert pending_owner_decision_holds(engine, now=NOW) == [raw_id]
    assert prepare_owner_decision_holds(engine, ReviewProvider(), _live, now=NOW)["prepared"] == 1
    bot = ReviewBot()
    await queue_owner_reviews(
        engine, bot, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT, enabled=True,
    )
    send = next(payload for method, payload in bot.calls if method == "sendMessage")
    assert send["link_preview_options"]["url"] == "https://www.beograd.rs/example.html"
    publisher = Publisher()
    with Session(engine) as session:
        review = session.scalar(select(Submission).where(Submission.kind == OWNER_REVIEW_KIND))
        review.status = "approved"
        run = session.get(EditorialRun, raw_id)
        package = _stored_package(run)
        session.commit()
        result = await deliver_owner_approved(
            session, review, publisher, live_documents=_live(package),
            target_channel_id=APPROVED_CHANNEL_ID, enabled=True,
        )
        assert result.message_id == 777
        assert publisher.calls[0].endswith("Источник: https://www.beograd.rs/example.html")


def test_media_cannot_support_a_high_risk_topic(engine):
    _undecided_event_hold(engine, ownership="media", category="law")
    provider = ReviewProvider()
    assert prepare_owner_decision_holds(engine, provider, _live, now=NOW) == {
        "prepared": 0, "blocked": 1,
    }
    assert provider.calls == []


def test_media_draft_that_copies_the_source_is_blocked_before_any_review():
    from relocant_media.services.owner_review_prepare import assert_primary_evidence

    text = " ".join(f"rec{n}" for n in range(30))
    draft = RussianDraft(title="Заголовок", body=text, claim_ids=["c1"])
    with pytest.raises(DeliveryBlockedError, match="copies"):
        assert_primary_evidence(["media"], "events", draft, ["Naslov " + text])
    assert assert_primary_evidence(["media"], "events", draft, ["drugi tekst"]) == "media"
    assert assert_primary_evidence(["official"], "law", draft, ["x " + text]) == "official"


@pytest.mark.asyncio
async def test_guide_reaches_owner_with_its_verification_date_and_publishes_after_checks(engine):
    raw_id = _undecided_event_hold(engine, guide=True, age=timedelta(days=12))
    assert pending_owner_decision_holds(engine, now=NOW) == [raw_id]
    assert prepare_owner_decision_holds(engine, ReviewProvider(), _live, now=NOW)["prepared"] == 1
    bot = ReviewBot()
    await queue_owner_reviews(
        engine, bot, owner_user_id=OWNER, owner_chat_id=OWNER_CHAT, enabled=True,
    )
    send = next(payload for method, payload in bot.calls if method == "sendMessage")
    assert "Проверено по официальному источнику: 23.10.2026" in send["text"]
    publisher = Publisher()
    with Session(engine) as session:
        review = session.scalar(select(Submission).where(Submission.kind == OWNER_REVIEW_KIND))
        review.status = "approved"
        package = _stored_package(session.get(EditorialRun, raw_id))
        session.commit()
        changed = tuple(
            document.model_copy(update={"body": document.body + " Izmena."})
            for document in _live(package)
        )
        with pytest.raises(DeliveryBlockedError):  # the live official page is checked again
            await deliver_owner_approved(
                session, review, publisher, live_documents=changed,
                target_channel_id=APPROVED_CHANNEL_ID, enabled=True,
            )
        result = await deliver_owner_approved(
            session, review, publisher, live_documents=_live(package),
            target_channel_id=APPROVED_CHANNEL_ID, enabled=True,
        )
        assert result.message_id == 777
        assert "Проверено по официальному источнику: 23.10.2026" in publisher.calls[0]


def test_guide_older_than_thirty_days_is_not_offered(engine):
    _undecided_event_hold(engine, guide=True, age=timedelta(days=31))
    assert pending_owner_decision_holds(engine, now=NOW) == []


def test_news_text_never_gets_a_verification_line(engine):
    raw_id = _undecided_event_hold(engine)
    prepare_owner_decision_holds(engine, ReviewProvider(), _live, now=NOW)
    with Session(engine) as session:
        from relocant_media.services.owner_review import _publication_text

        package = _stored_package(session.get(EditorialRun, raw_id))
        assert "Проверено" not in _publication_text(package, "official_city_news")


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


@pytest.mark.parametrize(
    ("published", "body"),
    [
        (NOW - timedelta(days=4), "Koncert se održava 20. oktobra 2026."),
        (NOW - timedelta(days=4), "Poziv dobrovoljnim davaocima krvi."),
        (NOW - timedelta(hours=1), "Koncert se održava 20. oktobra 2026."),
        (None, "Poziv dobrovoljnim davaocima krvi."),
        (NOW + timedelta(hours=1), "Poziv dobrovoljnim davaocima krvi."),
    ],
)
def test_owner_preparation_skips_unfresh_before_network_or_ai(engine, published, body):
    with Session(engine) as session:
        service, _, raw_id = prepare_service(session, category="politics")
        run = service.prepare(raw_id)
        original_outcome = json.loads(json.dumps(run.outcome))
        item = session.get(RawItem, raw_id)
        item.published_at = published
        item.body = body
        session.commit()
    provider = ReviewProvider()

    def forbidden_fetch(package):
        raise AssertionError("stale evidence must not reach the network")

    assert pending_owner_decision_holds(engine, now=NOW) == []
    assert prepare_owner_decision_holds(
        engine, provider, forbidden_fetch, now=NOW
    ) == {"prepared": 0, "blocked": 0}
    assert provider.calls == []
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        assert run.outcome == original_outcome
        assert run.delivery_status == "pending"


def test_owner_selection_keeps_older_explicit_upcoming_event(engine):
    with Session(engine) as session:
        service, _, raw_id = prepare_service(session, category="politics")
        service.prepare(raw_id)
        item = session.get(RawItem, raw_id)
        item.published_at = NOW - timedelta(days=4)
        item.body = "Koncert se održava 27. oktobra 2026."
        session.commit()
    assert pending_owner_decision_holds(engine, now=NOW) == [raw_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("age", [timedelta(hours=25), -timedelta(minutes=1)])
async def test_owner_publish_rejects_expired_or_future_reviews(engine, age):
    raw_id = _store_reviewed_politics(engine)
    publisher = Publisher()
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        package = _stored_package(run)
        package = package.model_copy(update={
            "reviews": tuple(
                review.model_copy(update={"reviewed_at": NOW - age})
                for review in package.reviews
            ),
        })
        run.outcome = {
            **run.outcome,
            "_package": package.model_dump(mode="json"),
            "_package_sha256": _package_hash(package),
        }
        review = _owner_review_row(session, raw_id)
        review.status = "approved"
        session.commit()

        with pytest.raises(DeliveryBlockedError, match="fresh independent approvals"):
            await deliver_owner_approved(
                session,
                review,
                publisher,
                live_documents=_live(package),
                target_channel_id=APPROVED_CHANNEL_ID,
                enabled=True,
            )
        assert publisher.calls == []
        session.refresh(run)
        assert run.delivery_status == "pending"
        assert session.get(Post, run.post_id).telegram_message_id is None
