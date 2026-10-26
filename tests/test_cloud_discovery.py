import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.services.cloud_discovery import (
    ORIGIN,
    batch_receipt,
    ingest_batch,
    parse_batch,
)
from relocant_media.services.submission_feedback import reply_to_submissions, resolve_submission
from relocant_media.services.submission_queue import batch_submissions, tip_urls
from relocant_media.storage.models import EditorialRun, Post, RawItem, Submission
from relocant_media.storage.sqlite_schema import initialize_sqlite

URL = "https://www.beograd.rs/cir/beoinfo-vesti/a116374/Radovi.html"


def payload(urls=None, **changes):
    return json.dumps({"version": 1, "city": "belgrade",
                       "urls": [URL] if urls is None else urls, **changes})


@pytest.fixture
def engine(tmp_path):
    path = tmp_path / "existing.sqlite3"
    initialize_sqlite(path)
    value = create_engine(f"sqlite:///{path}")
    yield value
    value.dispose()


@pytest.mark.parametrize("changes", [
    {"city": "novi_sad"}, {"version": 2}, {"version": True},
    {"ready": True}, {"reviews": []},
    {"urls": []}, {"urls": [URL] * 11}, {"urls": [123]},
])
def test_input_cannot_supply_policies_or_bypass_review(changes):
    with pytest.raises(ValidationError):
        parse_batch(payload(**changes))


@pytest.mark.parametrize("url", [
    "http://beograd.rs/a", "https://user:pass@beograd.rs/a", "https://localhost/a",
    "https://127.0.0.1/a", "https://foo.internal/a", "https://beograd.rs:8080/a",
    "https://beograd.rs/\nsecond", "https://beograd.rs/a https://other.example/b",
    "https://beograd.rs/" + "x" * 2000, "https://beograd.rs/a\" onclick=evil",
])
def test_malformed_or_private_pointers_are_rejected(url):
    with pytest.raises(ValueError):
        parse_batch(payload([url]))


def test_batch_size_is_bounded_before_json_parse():
    with pytest.raises(ValueError, match="exceeds bound"):
        parse_batch(" " * 24_001)


def test_import_uses_existing_submissions_and_never_creates_evidence_or_post(engine):
    with Session(engine) as session:
        ids, created = ingest_batch(session, parse_batch(payload()))
        session.commit()
        row = session.get(Submission, ids[0])
        assert created == 1 and row.status == "new"
        assert row.telegram_user_id is None and row.telegram_message_id is None
        assert tip_urls(row.payload) == [URL]
        assert row.payload["origin"] == ORIGIN
        assert row.payload["publication_evidence"] is False
        assert session.scalar(select(RawItem)) is None
        assert session.scalar(select(Post)) is None
        assert session.scalar(select(EditorialRun)) is None
        assert batch_submissions(session)[0].id == row.id
        receipt = batch_receipt(session, ids)[0]
        assert receipt["intake_status"] == "new"
        assert receipt["deliveries"] == []


def test_replay_is_idempotent_and_does_not_reset_resolved_state(engine):
    with Session(engine) as session:
        ids, _ = ingest_batch(session, parse_batch(payload()))
        session.commit()
        row = session.get(Submission, ids[0])
        row.status = "expired"
        row.payload = {**row.payload, "raw_item_ids": [123], "triage_reason": "expired"}
        session.commit()
        duplicate = URL + "?utm_source=telegram#section"
        repeated, created = ingest_batch(session, parse_batch(payload([duplicate, URL])))
        session.commit()
        assert repeated == ids and created == 0
        assert session.get(Submission, ids[0]).status == "expired"
        assert session.get(Submission, ids[0]).payload["raw_item_ids"] == [123]
        assert len(list(session.scalars(select(Submission)))) == 1


@pytest.mark.asyncio
async def test_cloud_candidates_never_get_private_telegram_replies(engine):
    class Bot:
        async def _call(self, *args, **kwargs):
            pytest.fail("cloud discovery has no Telegram recipient")

    with Session(engine) as session:
        ids, _ = ingest_batch(session, parse_batch(payload()))
        session.commit()
        resolve_submission(session.get(Submission, ids[0]), [], now=datetime.now(UTC))
        session.commit()
    assert await reply_to_submissions(engine, Bot(), enabled=True) == {
        "status": "ready", "sent": 0, "uncertain": 0, "blocked": 0,
        "unresolved": 0,
    }
