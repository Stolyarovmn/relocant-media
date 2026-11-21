"""Опоздавший GPT-ответ виден в логах и не выглядит как авария цикла."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_gpt_fallback_actions import PrivateStore, environment
from test_gpt_fallback_actions import context as _context
from test_gpt_fallback_actions import state as _state
from test_gpt_fallback_service import NOW, seed_fallback
from test_material_lifecycle_boundaries import close_material

from relocant_media.ai.package import canonical_json
from relocant_media.ops import actions_runtime as runtime
from relocant_media.ops.gpt_fallback_worker import _error_entry
from relocant_media.services.gpt_fallback import (
    FallbackValidationError,
    build_request,
    validate_fallback_candidate,
)
from relocant_media.services.material_lifecycle import LifecycleBlockedError, response_deadline
from relocant_media.storage.models import Base

context = _context
state = _state


@pytest.fixture
def file_database(tmp_path):
    path = tmp_path / "editorial.sqlite3"
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        raw, _ = seed_fallback(session)
        raw_id = raw.id
        request = build_request(session, now=NOW)
    yield path, engine, raw_id, request
    engine.dispose()


def _exchange(tmp_path, request, raw_id):
    path = tmp_path / "exchange.json"
    path.write_text(canonical_json({"request": request, "result": {
        "version": 1, "request_sha256": "0" * 64, "packages": [{"raw_item_id": raw_id}],
    }}), encoding="utf-8")
    return path


@pytest.mark.parametrize("expired", [False, True])
def test_closed_material_has_safe_reason_code(file_database, expired):
    _, engine, raw_id, request = file_database
    assert request is not None
    with Session(engine) as session:
        close_material(session, raw_id, now=NOW, expired=expired)
        with pytest.raises(FallbackValidationError) as caught:
            validate_fallback_candidate(session, request, raw_id, now=NOW)
    assert caught.value.code == "material_closed"
    assert _error_entry("validate", caught.value) == {
        "stage": "validate", "error_type": "FallbackValidationError",
        "reason": "material_closed",
    }


def test_error_entry_keeps_plain_errors_unchanged():
    assert _error_entry("live", ValueError("PRIVATE")) == {
        "stage": "live", "error_type": "ValueError"}
    assert _error_entry("validate", LifecycleBlockedError("PRIVATE"))["reason"] == (
        "lifecycle_blocked")


def test_failure_coordinates_include_only_safe_processing_errors(capsys):
    runtime.print_failure_coordinates({
        "status": "PRIMARY_FAILED",
        "processing_errors": [
            {"stage": "validate", "error_type": "FallbackValidationError",
             "reason": "material_closed", "message": "PRIVATE-TEXT"},
            {"stage": "bad stage with spaces", "error_type": "ValueError"},
            "not-a-dict",
        ],
    })
    err = capsys.readouterr().err
    assert "PRIVATE-TEXT" not in err
    assert json.loads(err)["runtime_failure"] == {
        "status": "PRIMARY_FAILED",
        "processing_errors": [
            {"stage": "validate", "error_type": "FallbackValidationError",
             "reason": "material_closed"},
            {"error_type": "ValueError"},
        ],
    }


@pytest.mark.parametrize("closed", [False, True])
def test_expired_result_is_detected_read_only(file_database, tmp_path, closed):
    path, engine, raw_id, request = file_database
    if closed:
        with Session(engine) as session:
            close_material(session, raw_id, now=NOW, expired=True)
    before = path.read_bytes()
    exchange = _exchange(tmp_path, request, raw_id)
    assert runtime._gpt_result_expired(path, exchange, NOW) is closed
    assert path.read_bytes() == before


def test_unknown_material_is_left_to_the_worker(file_database, tmp_path):
    path, _, _, request = file_database
    assert runtime._gpt_result_expired(path, _exchange(tmp_path, request, 999_999), NOW) is False


def test_response_deadline_uses_the_earliest_window(file_database):
    _, engine, raw_id, _ = file_database
    with Session(engine) as session:
        assert response_deadline(session, raw_id) is None
        record = close_material(session, raw_id, now=NOW, expired=True)
        material = NOW + timedelta(hours=1)
        record.details = {**record.details, "deadline_at": material.isoformat()}
        session.commit()
        assert response_deadline(session, raw_id) == material
        fallback = NOW + timedelta(minutes=20)
        record.details = {**record.details, "state": "waiting_fallback",
                          "next_action": "fallback", "fallback_deadline_at": fallback.isoformat()}
        session.commit()
        assert response_deadline(session, raw_id) == fallback


def test_expired_fallback_exits_before_claim(context, tmp_path, monkeypatch, capsys):
    store, old = context
    storage = PrivateStore(old.files)
    request = {"version": 1, "candidates": [{"raw_item_id": 23}]}
    request_sha = storage.save(canonical_json(request).encode())
    result = {"version": 1, "request_sha256": request_sha, "packages": [{"raw_item_id": 23}]}
    result_sha = storage.save(canonical_json(result).encode())
    monkeypatch.setattr(runtime, "_gpt_result_expired", lambda *args: True)
    monkeypatch.setattr(runtime, "run_child", lambda *args: pytest.fail("unexpected worker"))
    code = runtime.execute(store, storage, work=tmp_path / "private",
                           output=tmp_path / "data.json", folder="ssh_blobs", mode="fallback",
                           environment={**environment(), "GPT_FALLBACK_BLOB_SHA256": result_sha})
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {
        "gpt_fallback": "result_expired", "reason": "material_closed"}
    assert store.state["status"] == "ready" and store.sha == "initial"


@pytest.mark.parametrize("minutes", [5, 25])
def test_preview_refuses_request_without_time_to_answer(
    context, tmp_path, monkeypatch, capsys, minutes,
):
    from relocant_media.services import gpt_fallback

    store, old = context
    storage = PrivateStore(old.files)
    request = {"version": 1, "candidates": [{"raw_item_id": 23}]}
    monkeypatch.setattr(gpt_fallback, "build_request", lambda *args, **kwargs: request)
    deadline = datetime.now(UTC) + timedelta(minutes=minutes)
    monkeypatch.setattr(runtime, "_request_deadlines", lambda *args: [deadline])
    files = dict(storage.files)
    assert runtime.execute(store, storage, work=tmp_path / "preview",
                           output=tmp_path / "public.json", folder="ssh_blobs",
                           mode="fallback-preview", environment=environment()) == 0
    receipt = json.loads(capsys.readouterr().out)
    if minutes < 10:
        assert receipt == {"gpt_fallback": "deadline_too_close",
                           "respond_by": deadline.isoformat()}
        assert storage.files == files
    else:
        assert receipt["respond_by"] == deadline.isoformat()
        assert storage.download(receipt["gpt_fallback_request_sha256"]) == canonical_json(
            request).encode()
    assert store.state["status"] == "ready" and store.sha == "initial"
