from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_reader_publication import POLICY, material
from test_reader_resolution import READER_ID, Bot, submission
from test_reader_resolution import engine as reader_engine
from test_venue_runtime import NOW
from test_venue_runtime import runtime as venue_runtime

from relocant_media.ops import server_cycle
from relocant_media.services.reader_publication import material_fingerprint, permission_reason
from relocant_media.services.reader_resolution import process_reader_resolutions
from relocant_media.services.reader_source_discovery import (
    DISCOVERY_FIELD,
    matches_reader_event,
    pending_source_discovery,
    record_discovery_check,
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
