from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_reader_publication import POLICY, material
from test_reader_resolution import OWNER_ID, READER_ID, Bot, callback, submission
from test_reader_resolution import engine as reader_engine
from test_venue_runtime import NOW, URL
from test_venue_runtime import runtime as venue_runtime

from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.collectors.telegram_topics import TopicLead
from relocant_media.ops import server_cycle
from relocant_media.services.reader_publication import material_fingerprint, permission_reason
from relocant_media.services.reader_resolution import (
    process_reader_resolutions,
    record_reader_resolution_callback,
)
from relocant_media.services.reader_source_discovery import (
    DISCOVERY_FIELD,
    matches_reader_event,
    pending_source_discovery,
    record_discovery_check,
    review_candidates,
    undated_reader_match,
)
from relocant_media.services.submission_queue import priority_raw_ids
from relocant_media.storage.models import RawItem, Submission

engine = reader_engine
runtime = venue_runtime


def source_signal(text):
    payload = material(mode="anonymous", permission="private")
    payload["text"] = text
    payload["reader_publication"].update(
        ownership="source", material_sha256=material_fingerprint(payload),
    )
    return payload


@pytest.mark.asyncio
async def test_private_signal_links_only_matching_public_feed(runtime):
    engine, _, _, contents, requests = runtime
    contents.update(title="Mango Night Market u muzeju", body=(
        "Mango Night Market se održava 25. oktobra 2026. u muzeju od 18 časova. " * 5
    ))
    matched = submission(engine, status="reader_review", **source_signal(
        "25 октября состоится Mango Night Market.\nЗакрытая деталь private-label-492."
    ))
    different = submission(engine, status="reader_review", **source_signal(
        "26 октября состоится Mango Night Market."
    ))
    assert (await server_cycle.collect_sources(engine, submissions_only=True))[
        "reader_source_discovery"]["checked"] == 0
    result = await server_cycle.collect_sources(engine)
    assert result["reader_source_discovery"] == {"checked": 2, "linked": 1, "ambiguous": 0}
    with Session(engine) as session:
        linked = session.get(Submission, matched)
        assert linked.status == "source_linked"
        assert permission_reason(linked.payload, POLICY["channel_id"])
        assert "reader_media_review" not in linked.payload
        assert session.get(Submission, different).status == "reader_review"
        assert session.get(Submission, different).payload[DISCOVERY_FIELD]["status"] == "no_match"
        raw_id, = priority_raw_ids(session)
        assert "private-label" not in session.get(RawItem, raw_id).body
    assert not any("private-label" in url for url in requests)


@pytest.mark.asyncio
@pytest.mark.parametrize("overflow", [False, True])
async def test_forward_checks_enabled_channel_and_links_verified_article(
    runtime, monkeypatch, overflow,
):
    engine, venue, _, contents, _ = runtime
    title = "Mango Night Market u muzeju"
    body = "Mango Night Market se održava 25. oktobra 2026. u muzeju od 18 časova. " * 5
    contents.update(title=title, body=body)
    ids = {}
    for username in ("serbia", "notregistered"):
        payload = source_signal("Анонс из канала\nЗакрытая деталь.")
        posts = range(193, 198) if overflow and username == "serbia" else (197,)
        payload["reader_parts"] = {
            str(post): {"public_forward_url": f"https://t.me/{username}/{post}"}
            for post in posts
        }
        payload["reader_publication"]["material_sha256"] = material_fingerprint(payload)
        ids[username] = submission(engine, status="reader_review", **payload)
    forwarded = []

    async def exact_post(channel, message_id, client, *, now):
        forwarded.append((channel, message_id))
        if message_id != 197:
            return None
        return TopicLead(
            channel=channel, message_id=message_id, url=f"https://t.me/{channel}/{message_id}",
            published_at=now - timedelta(hours=2), observed_at=now,
            text="Отдельная публичная публикация", outbound_urls=(URL,), views=None,
        )

    async def article(_, url):
        assert url == URL
        return CollectedItem(venue.id, "verified", URL, title, body, NOW - timedelta(hours=2))

    monkeypatch.setattr(server_cycle, "runtime_channels_for_city", lambda _: ("Serbia",))
    monkeypatch.setattr(server_cycle, "collect_forwarded_topic", exact_post)
    monkeypatch.setattr(server_cycle, "_collect_exact_url", article)
    result = await server_cycle.collect_sources(engine)
    if overflow:
        assert forwarded == [("Serbia", post) for post in range(193, 197)]
        with Session(engine) as session:
            waiting = session.get(Submission, ids["serbia"])
            assert waiting.payload[DISCOVERY_FIELD]["status"] == "forward_lookup_deferred"
        forwarded.clear()
        result = await server_cycle.collect_sources(engine)
    assert forwarded == [("Serbia", 197)]
    assert result["reader_source_discovery"]["linked"] == 1
    assert result["reader_source_discovery"]["ambiguous"] == 0
    with Session(engine) as session:
        linked = session.get(Submission, ids["serbia"])
        assert linked.status == "source_linked"
        raw_id, = linked.payload["raw_item_ids"]
        item = session.get(RawItem, raw_id)
        assert item.url == URL
        assert "Закрытая деталь" not in item.body
        assert session.get(Submission, ids["notregistered"]).status == "reader_review"


def test_retry_window_respects_delays_original_policy_and_new_material(engine):
    def pending(session, now):
        return [row.id for row in pending_source_discovery(
            session, city="belgrade", now=now, has_article_link=lambda _: False,
        )]

    sid = submission(engine, status="reader_review", **source_signal("Сигнал"))
    private_own = material(mode="anonymous", permission="private")
    submission(engine, status="reader_review", **private_own)
    with Session(engine) as session:
        assert pending(session, NOW) == [sid]
        row = session.get(Submission, sid)
        record_discovery_check(row, now=NOW, status="no_match")
        session.commit()
        assert pending(session, NOW + timedelta(hours=5, minutes=59)) == []
        assert pending(session, NOW + timedelta(hours=6)) == [sid]
        row.payload = {**row.payload, "text": "Прислано уточнение к материалу"}
        session.commit()
        assert pending(session, NOW + timedelta(hours=6)) == []


@pytest.mark.asyncio
async def test_unconfirmed_signal_closes_and_notifies_reader_once_after_window(engine):
    now = datetime.now(UTC)
    payload = source_signal("Mango Night Market 11 декабря")
    payload.update(chat_id=READER_ID, accepted_at=(now - timedelta(hours=49)).isoformat())
    sid = submission(engine, status="reader_review", **payload)
    with Session(engine) as session:
        row = session.get(Submission, sid)
        record_discovery_check(row, now=now, status="no_match")
        session.commit()
    bot = Bot()
    await process_reader_resolutions(engine, bot, enabled=True)
    await process_reader_resolutions(engine, bot, enabled=True)
    assert len(bot.sent) == 1
    assert bot.sent[0][1]["chat_id"] == READER_ID
    assert "не нашли достаточного публичного подтверждения" in bot.sent[0][1]["text"]
    with Session(engine) as session:
        row = session.get(Submission, sid)
        assert row.status == "closed"
        assert row.payload["reader_resolution"]["outcome"] == "public_confirmation_missing"


def test_reader_matches_same_date_in_russian_and_serbian_cyrillic():
    tip = "Музичко-поетски кабаре «Да, то су били дани»\n24 ноября на сцене Чукарицы."
    title = "Музичко-поетски кабаре „Да, то су били дани“ на сцени Чукарице"
    assert matches_reader_event(tip, title, "24. новембра", year=2026, source_year=2026)
    assert not matches_reader_event(tip, title, "25. новембра", year=2026, source_year=2026)
    assert not matches_reader_event(tip, title, "24. новембра", year=2026, source_year=2025)


def test_match_uses_event_date_before_ticket_sale_date():
    title = "Представа СумЉиво лице на сцени Културног центра Чукарица"
    body = "Представа је 30. новембра.\n\nКарте могу се преузети од 15. новембра."
    assert matches_reader_event(f"{title} 30 ноября", title, body, year=2026, source_year=2026)
    assert not matches_reader_event(f"{title} 15 ноября", title, body, year=2026, source_year=2026)


_TITLE = 'Program festivala „Mango Night Market“'
_OPENING = 'Festival se održava 25. oktobra 2026.\nLokacija: Muzej Savremene Umetnosti'
_SIGNAL = 'Фестиваль «Mango Night Market».\nПлощадка: Muzej Savremene Umetnosti'


def test_undated_match_checks_the_window_named_venue_and_event():
    timing = {"submitted_at": NOW, "published_at": NOW - timedelta(hours=12)}
    assert undated_reader_match(_SIGNAL, _TITLE, _OPENING, **timing) == "anchored"
    assert undated_reader_match(_SIGNAL + "\nЗавтра", _TITLE, _OPENING, **timing) == "review"
    assert undated_reader_match(
        _SIGNAL + "\nЦена 500", _TITLE, _OPENING + "\nCena 800", **timing,
    ) is None
    assert undated_reader_match(
        _SIGNAL.replace("Muzej Savremene Umetnosti", "Muzej Istorije Srbije"), _TITLE,
        _OPENING, **timing,
    ) is None
    timing["published_at"] = NOW - timedelta(hours=49)
    assert undated_reader_match(_SIGNAL, _TITLE, _OPENING, **timing) is None


@pytest.mark.asyncio
async def test_undated_media_signal_shows_source_only_to_editor(runtime, monkeypatch):
    engine, venue, _, contents, requests = runtime
    media = venue.model_copy(update={"source_type": "news_media", "ownership": "media"})
    monkeypatch.setattr(server_cycle, "venue_sources", dict)
    monkeypatch.setattr(server_cycle, "media_sources", lambda: {media.id: media})
    contents.update(title=_TITLE, body=_OPENING + "\n" + (
        "Mango Night Market je program festivala savremene umetnosti u muzeju." * 5
    ))
    matched = submission(engine, status="reader_review", accepted_at=NOW.isoformat(),
                         **source_signal(_SIGNAL))
    review = submission(engine, status="reader_review", accepted_at=NOW.isoformat(),
                        **source_signal('Mango Night Market festivala savremene.\nprivate-765'))

    result = await server_cycle.collect_sources(engine)
    assert result["reader_source_discovery"] == {"checked": 2, "linked": 1, "ambiguous": 0}
    owner = {"owner_user_id": OWNER_ID, "owner_chat_id": OWNER_ID}
    bot = Bot()
    await process_reader_resolutions(engine, bot, enabled=True, **owner)
    assert not any("private-765" in url for url in requests)
    assert not any("private-765" in call["text"] for _, call in bot.sent
                   if call["chat_id"] != OWNER_ID)
    with Session(engine) as session:
        linked = session.get(Submission, matched)
        row = session.get(Submission, review)
        candidate, = review_candidates(row)
        assert row.status == "reader_review"
        assert linked.payload["raw_item_ids"] == [candidate["id"]]
        assert permission_reason(row.payload, POLICY["channel_id"])
        card_count, card = next((index, call)
                                for index, (_, call) in enumerate(bot.sent, start=1)
                                if "private-765" in call["text"])
        assert card["protect_content"]
        assert candidate["url"] in card["text"]
        button = card["reply_markup"]["inline_keyboard"][0][0]
        assert button["text"] == "Проверить источник 1"

        action = callback(review, "candidate", message_id=50 + card_count)
        action["data"] = button["callback_data"]
        assert record_reader_resolution_callback(
            session, {**action, "from": {"id": READER_ID}}, **owner,
        ) is None
        outdated = {**action, "data": action["data"].rsplit(":", 1)[0] + ":0000000000"}
        assert not record_reader_resolution_callback(session, outdated, **owner).accepted
        original = row.payload
        row.payload = {**row.payload, "text": "Изменилась версия сообщения"}
        assert not record_reader_resolution_callback(session, action, **owner).accepted
        row.payload = original
        assert record_reader_resolution_callback(session, action, **owner).accepted
        assert row.status == "source_linked"
        assert row.payload["raw_item_ids"] == [candidate["id"]]
        assert permission_reason(row.payload, POLICY["channel_id"])
