"""Поздний публичный readback подтверждённого API не разрешает второй edit."""

import copy
import json
import sqlite3
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_correction_delivery import NOW, embed
from test_exact_text_correction import Remote, execute, seal
from test_exact_text_correction import exact as exact
from test_exact_text_correction import frozen as frozen
from test_exact_text_correction import ready_package as ready_package
from test_exact_text_correction import seeded as seeded

from relocant_media.ai.package import canonical_json
from relocant_media.ops import correction_delivery as worker
from relocant_media.services.published_text import effective_published_text
from relocant_media.storage.models import EditorialRun, Post


class CachedRemote(Remote):
    readonly = False

    def __call__(self, request):
        if request.method != "GET" and self.readonly:
            pytest.fail("Поздняя сверка не имеет права вызывать POST/edit")
        response = super().__call__(request)
        if request.method == "POST":
            self.corrected = self.current
            self.current = embed(self.exact["old"], self.exact["plan"]["source_url"])
        return response


def replay(exact, path, remote, *, now=None):
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        # Для read-only пути токен не нужен и не передаётся в функцию сверки.
        return worker.execute(exact["storage"], exact["sha"], path, client,
                              token="NO_EDIT_CAPABILITY", now=now or NOW + timedelta(minutes=10))


@pytest.fixture
def stale(exact, tmp_path):
    remote = CachedRemote(exact)
    assert execute(exact, tmp_path / "initial", remote)["status"] == "uncertain"
    storage = exact["storage"]
    assert storage.audits[-1]["api_confirmed"] is True
    assert storage.audits[-1]["public_confirmed"] is False
    assert storage.audits[-1]["public_text_sha256"] == exact["plan"]["old_text_sha256"]
    remote.readonly = True
    remote.current = remote.corrected
    return exact, remote


def target(storage):
    return next(iter(storage.state["exact_text_corrections"].values()))


def store_record(storage, record):
    sha = worker._digest(record)
    storage.blobs[sha] = canonical_json(record).encode()
    return sha


@pytest.mark.parametrize("elapsed", [timedelta(minutes=10), timedelta(hours=3)])
def test_late_readback_preserves_api_proof_without_second_post(stale, tmp_path, elapsed):
    exact, remote = stale
    storage = exact["storage"]
    previous = copy.deepcopy(target(storage))
    old_bytes = storage.blobs[previous["receipt_sha256"]]
    old_receipt = json.loads(old_bytes)
    # Независимый writer мог сменить generation после исходного edit.
    storage.state["generation"] = "later-canonical-generation"
    assert replay(exact, tmp_path / "reconcile", remote, now=NOW + elapsed)["status"] == "corrected"
    current = target(storage)
    assert current["attempts"] == 1 and len(remote.posts) == 1
    assert current["receipt_sha256"] != previous["receipt_sha256"]
    new_receipt = json.loads(storage.blobs[current["receipt_sha256"]])
    assert new_receipt["previous_receipt_sha256"] == previous["receipt_sha256"]
    assert new_receipt["previous_receipt"] == old_receipt
    assert storage.blobs[previous["receipt_sha256"]] == old_bytes
    assert new_receipt["api_confirmed"] is old_receipt["api_confirmed"] is True
    assert new_receipt["public_confirmed"] is True
    assert new_receipt["reconciled_at"] == (NOW + elapsed).isoformat()
    assert new_receipt["checked_at"] == old_receipt["checked_at"]
    assert exact["database"].read_bytes() == exact["before"]
    database = tmp_path / "corrected.sqlite3"
    database.write_bytes(storage.blobs[storage.state["snapshot"]["sha256"]])
    engine = create_engine("sqlite://", creator=lambda: sqlite3.connect(database))
    try:
        with Session(engine) as session:
            run = session.scalar(select(EditorialRun))
            post = session.get(Post, run.post_id)
            assert effective_published_text(post, run).corrected
            broken = copy.deepcopy(run.outcome["_published_correction"])
            broken["receipt"]["previous_receipt"]["api_confirmed"] = False
            broken["receipt"]["previous_receipt_sha256"] = worker._digest(
                broken["receipt"]["previous_receipt"])
            broken["receipt_sha256"] = worker._digest(broken["receipt"])
            run.outcome = {**run.outcome, "_published_correction": broken}
            assert not effective_published_text(post, run).corrected
            # Частично удалённая chain metadata не становится обычной corrected-квитанцией.
            del broken["receipt"]["previous_receipt_sha256"]
            broken["receipt_sha256"] = worker._digest(broken["receipt"])
            run.outcome = {**run.outcome, "_published_correction": broken}
            assert not effective_published_text(post, run).corrected
    finally:
        engine.dispose()
    assert replay(exact, tmp_path / "again", remote,
                  now=NOW + timedelta(hours=4))["status"] == "already_corrected"
    assert len(remote.posts) == 1 and len(storage.writes) == 4


@pytest.mark.parametrize("ack", [False, None, 1])
def test_unknown_or_non_boolean_api_ack_never_promotes_even_exact_public_text(stale, tmp_path, ack):
    exact, remote = stale
    storage = exact["storage"]
    receipt = json.loads(storage.blobs[target(storage)["receipt_sha256"]])
    if ack is None:
        del receipt["api_confirmed"]
    else:
        receipt["api_confirmed"] = ack
    target(storage)["receipt_sha256"] = store_record(storage, receipt)
    before = copy.deepcopy(storage.state)
    assert replay(exact, tmp_path / "blocked", remote,
                  now=NOW + timedelta(hours=3))["status"] == "uncertain"
    assert storage.state == before and len(storage.writes) == 2 and len(remote.posts) == 1


@pytest.mark.parametrize("mutation", ["receipt_hash", "intent_hash", "recipient", "payload",
    "plan", "checkpoint", "ledger", "counter", "public", "public_source", "source", "backdate"])
def test_changed_proof_or_public_state_does_not_release_fence(stale, tmp_path, mutation):
    exact, remote = stale
    storage = exact["storage"]
    previous = target(storage)
    intent = json.loads(storage.blobs[previous["intent_sha256"]])
    receipt = json.loads(storage.blobs[previous["receipt_sha256"]])
    if mutation in {"receipt_hash", "intent_hash"}:
        storage.blobs[previous[mutation.replace("_hash", "_sha256")]] += b" "
    elif mutation in {"recipient", "payload", "plan", "backdate"}:
        field = {"recipient": "chat_id", "payload": "payload_sha256", "plan": "plan_sha256"}[
            mutation] if mutation != "backdate" else "checked_at"
        value = (intent[field] + 1 if field == "chat_id" else
                 (NOW - timedelta(minutes=1)).isoformat() if field == "checked_at" else "0" * 64)
        intent[field] = receipt[field] = value
        if mutation == "backdate":
            previous["recorded_at"] = value
        previous["intent_sha256"] = store_record(storage, intent)
        receipt["intent_sha256"] = previous["intent_sha256"]
        previous["receipt_sha256"] = store_record(storage, receipt)
    elif mutation == "checkpoint":
        checkpoint = json.loads(storage.blobs[receipt["checkpoint_sha256"]])
        checkpoint["state"]["snapshot"]["sha256"] = "0" * 64
        receipt["checkpoint_sha256"] = store_record(storage, checkpoint)
        previous["receipt_sha256"] = store_record(storage, receipt)
    elif mutation == "ledger":
        previous["unexpected_change"] = True
    elif mutation == "counter":
        previous["attempts"] = 2
    elif mutation == "public":
        remote.current = embed("Другой текст\n\nИсточник", exact["plan"]["source_url"])
    elif mutation == "public_source":
        remote.current = embed(remote.posts[0]["text"], "https://different.example/source")
    else:
        url = exact["plan"]["source_url"]
        exact["sources"][url] = exact["sources"][url].replace("</article>", "Изменено</article>")
    before = copy.deepcopy(storage.state)
    if mutation in {"public", "public_source"}:
        assert replay(exact, tmp_path / "blocked", remote)["status"] == "uncertain"
    else:
        with pytest.raises(ValueError):
            replay(exact, tmp_path / "blocked", remote)
    assert storage.state == before and len(storage.writes) == 2 and len(remote.posts) == 1


@pytest.mark.parametrize("failure", ["fail_claim", "unknown_claim", "fail_finish"])
def test_reconcile_cas_failure_and_checkpoint_replay_never_edit(stale, tmp_path, failure):
    exact, remote = stale
    storage = exact["storage"]
    setattr(storage, failure, True)
    with pytest.raises(ValueError):
        replay(exact, tmp_path / "reconcile", remote)
    assert len(remote.posts) == 1
    if failure != "fail_claim":
        assert storage.state["status"] == "busy"
        with pytest.raises(ValueError):
            replay(exact, tmp_path / "blocked", remote)
        # Только явное ручное восстановление сохранённой контрольной точки.
        storage.state = copy.deepcopy(storage.checkpoints[-1]["state"])
        if failure == "fail_finish":
            assert replay(exact, tmp_path / "already", remote)["status"] == "already_corrected"
    assert len(remote.posts) == 1


def test_changed_plan_cannot_reuse_previous_api_ack(stale, tmp_path):
    exact, remote = stale
    exact["plan"]["body"] += "\nДругая редакция."
    exact["sha"] = seal(exact["storage"], exact["plan"])
    with pytest.raises(ValueError, match="другое намерение"):
        replay(exact, tmp_path / "blocked", remote)
    assert len(remote.posts) == 1 and len(exact["storage"].writes) == 2


def test_original_edit_still_requires_fresh_reviews(exact, tmp_path):
    remote = CachedRemote(exact)
    with pytest.raises(ValueError):
        replay(exact, tmp_path / "expired", remote, now=NOW + timedelta(hours=3))
    assert not remote.posts and not exact["storage"].writes
