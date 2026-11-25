"""Экспорт использует только зафиксированный снимок и не раскрывает приватный payload."""

import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_actions_runtime import context as _context
from test_actions_runtime import state as _state
from test_gpt_fallback_actions import PrivateStore, environment
from test_gpt_fallback_service import NOW, seed_fallback

from relocant_media.ai.package import canonical_json
from relocant_media.ops import actions_runtime as runtime
from relocant_media.ops.gpt_export_receipt import (
    EXPORT_KIND,
    marker_from_cycle_job,
    validate_export_marker,
    validate_request_binding,
)
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import gpt_fallback
from relocant_media.services.material_lifecycle import adopt_fallback_request, response_deadline
from relocant_media.storage.models import Base, RawItem, Source

context, state = _context, _state


def hosted_environment():
    return {**environment(), "GITHUB_ACTIONS": "true", "GITHUB_RUN_ID": "12345",
            "GITHUB_RUN_ATTEMPT": "2", "GITHUB_JOB": "cycle", "GITHUB_SHA": "a"*40,
            "DRAIN_BEFORE": "2000-01-01T00:00:00+00:00", "DRAIN_RUNS_LEFT": "1"}


def request():
    return {"version": 1, "scheduler_run_id": 17,
            "candidates": [{"raw_item_id": 23, "body": "PRIVATE-NEVER-IN-LOGS",
                            "documents": [{"ownership": "official", "high_risk": False,
                                           "auto_publish": True}]}]}


def configure(context, monkeypatch, *, built=None, worker_code=0, export_error=None,
              worker_run_id=None):
    store, old = context
    storage = PrivateStore(old.files)
    operations = []
    real_upload = storage.upload

    def upload(path, folder):
        if folder == "gpt-fallback-request":
            assert store.state["status"] == "ready"
            operations.append("upload_after_commit")
            if export_error == "failure":
                raise OSError("PRIVATE-NEVER-IN-LOGS")
            if export_error == "digest":
                return "f"*64
        return real_upload(path, folder)

    def build(session, **kwargs):
        assert store.state["status"] == "ready"
        assert session.connection().exec_driver_sql("PRAGMA query_only").scalar() == 1
        database = Path(session.get_bind().url.database)
        assert database.is_file() and database.parent.name == "saved"
        assert hashlib.sha256(database.read_bytes()).hexdigest() == (
            store.state["snapshot"]["sha256"])
        operations.append("build_after_commit")
        return built(kwargs.get("scheduler_run_id")) if callable(built) else built

    def child(command, env):
        assert store.state["status"] == "busy"
        directory = Path(command[-1])
        directory.mkdir()
        (directory / "server-report.json").write_text(json.dumps({
            "status": "PRIMARY_PARTIAL", "run_id": worker_run_id,
            "processing_errors": [{"error_type": "GeminiWorkerError"}],
        }), encoding="utf-8")
        if "relocant_media.ops.backlog_drain" in command:
            (directory / "drain-report.json").write_text(json.dumps({
                "status": "DRAIN_OK", "remaining": 1, "needs_continue": True,
                "next_delay_seconds": 60, "next_at": None}), encoding="utf-8")
        return worker_code

    monkeypatch.setattr(storage, "upload", upload)
    monkeypatch.setattr(gpt_fallback, "build_request", build)
    monkeypatch.setattr(runtime, "_require_public_fallback_documents", lambda *a: None)
    monkeypatch.setattr(runtime, "run_child", child)
    monkeypatch.setattr(runtime, "export_public", lambda *a: None)
    return store, storage, operations


@pytest.mark.parametrize("mode", ["editorial", "auto", "drain"])
def test_partial_success_exports_same_v1_after_commit_without_second_restore(
    context, tmp_path, monkeypatch, capsys, mode,
):
    payload = request()
    store, storage, operations = configure(context, monkeypatch, built=payload)
    assert runtime.execute(store, storage, work=tmp_path / "work", output=tmp_path / "data.json",
        folder="ssh_blobs", mode=mode, environment=hosted_environment()) == 0
    captured = capsys.readouterr()
    assert "PRIVATE-NEVER-IN-LOGS" not in captured.out+captured.err
    marker = next(json.loads(line) for line in captured.out.splitlines()
                  if json.loads(line).get("gpt_fallback_export") == EXPORT_KIND)
    validate_export_marker(marker)
    assert marker["scheduler_run_id"] == payload["scheduler_run_id"]
    assert marker["candidate_count"] == 1 and marker["worker_mode"] == (
        "drain" if mode == "drain" else "editorial")
    assert marker["snapshot_sha256"] == store.state["snapshot"]["sha256"]
    assert marker["generation"] == store.state["generation"]
    assert storage.download(marker["gpt_fallback_request_sha256"]) == (
        canonical_json(payload).encode())
    assert operations == ["build_after_commit", "upload_after_commit"]
    assert '"runtime_phase": "state_committed"' in captured.err


def test_no_candidate_does_not_upload_or_emit_success(context, tmp_path, monkeypatch, capsys):
    store, storage, operations = configure(context, monkeypatch)
    assert runtime.execute(store, storage, work=tmp_path / "work", output=tmp_path / "data.json",
        folder="ssh_blobs", mode="editorial", environment=hosted_environment()) == 0
    logs = capsys.readouterr().out
    assert EXPORT_KIND not in logs and "no_pending_candidates" in logs
    assert operations == ["build_after_commit"] and store.state["status"] == "ready"


@pytest.mark.parametrize("eligible", [True, False])
def test_automatic_export_prefers_its_saved_cycle_then_backlog(
    context, tmp_path, monkeypatch, capsys, eligible,
):
    current = {**request(), "scheduler_run_id": 21}
    old = request()
    considered = []

    def build(scheduler_run_id):
        considered.append(scheduler_run_id)
        if scheduler_run_id == current["scheduler_run_id"]:
            return current if eligible else None
        assert scheduler_run_id is None
        return old

    store, storage, _ = configure(
        context, monkeypatch, built=build, worker_run_id=current["scheduler_run_id"],
    )
    assert runtime.execute(store, storage, work=tmp_path / "work", output=tmp_path / "data.json",
        folder="ssh_blobs", mode="auto", environment=hosted_environment()) == 0
    expected_order = [21] if eligible else [21, None]
    assert considered == expected_order
    marker = next(json.loads(line) for line in capsys.readouterr().out.splitlines()
                  if json.loads(line).get("gpt_fallback_export") == EXPORT_KIND)
    expected = current if eligible else old
    assert marker["scheduler_run_id"] == expected["scheduler_run_id"]
    assert storage.download(marker["gpt_fallback_request_sha256"]) == canonical_json(
        expected,
    ).encode()


@pytest.mark.parametrize("error", ["failure", "digest"])
def test_optional_export_failure_preserves_commit_and_never_announces_success(
    context, tmp_path, monkeypatch, capsys, error,
):
    store, storage, _ = configure(context, monkeypatch, built=request(), export_error=error)
    assert runtime.execute(store, storage, work=tmp_path / "work", output=tmp_path / "data.json",
        folder="ssh_blobs", mode="editorial", environment=hosted_environment()) == 0
    logs = capsys.readouterr()
    assert EXPORT_KIND not in logs.out
    assert "gpt_fallback_export_error_type" in logs.err
    assert "PRIVATE-NEVER-IN-LOGS" not in logs.out+logs.err
    assert store.state["status"] == "ready" and store.state["last_exit_code"] == 0


def test_failed_commit_never_builds_uploads_or_announces(context, tmp_path, monkeypatch, capsys):
    store, storage, operations = configure(context, monkeypatch, built=request())
    store.fail_commit = True
    with pytest.raises(ValueError, match="conflict"):
        runtime.execute(store, storage, work=tmp_path / "work", output=tmp_path / "data.json",
            folder="ssh_blobs", mode="editorial", environment=hosted_environment())
    assert operations == [] and EXPORT_KIND not in capsys.readouterr().out


def test_failed_worker_keeps_legacy_preview_path(context, tmp_path, monkeypatch, capsys):
    store, storage, operations = configure(context, monkeypatch, built=request(), worker_code=1)
    assert runtime.execute(store, storage, work=tmp_path / "work", output=tmp_path / "data.json",
        folder="ssh_blobs", mode="editorial", environment=hosted_environment()) == 1
    assert store.state["status"] == "ready" and operations == []
    assert EXPORT_KIND not in capsys.readouterr().out


def marker():
    return {"gpt_fallback_export": EXPORT_KIND, "gpt_fallback_request_sha256": "b"*64,
            "candidate_count": 1, "scheduler_run_id": 17, "snapshot_sha256": "c"*64,
            "generation": "d"*32, "workflow_run_id": 12345, "workflow_run_attempt": 2,
            "head_sha": "a"*40, "job_key": "cycle", "worker_mode": "editorial"}


def metadata():
    run = {"id": 12345, "run_attempt": 2, "repository": {"full_name": runtime.REPO},
           "head_branch": "main", "head_sha": "a"*40, "event": "schedule",
           "path": ".github/workflows/editorial-actions.yml", "status": "in_progress",
           "conclusion": None}
    job = {"id": 54321, "run_id": 12345, "run_attempt": 2, "name": "cycle",
           "head_sha": "a"*40, "status": "completed", "conclusion": "success"}
    return run, job


def logs(value=None):
    return ('prefix {"runtime_phase":"state_commit_started"}\n'
            'prefix {"runtime_phase":"state_committed"}\n'
            + "prefix "+json.dumps(value or marker())+"\n").encode()


@pytest.mark.parametrize("status", ["in_progress", "queued", "pending", "waiting", "requested"])
def test_successful_cycle_can_be_read_while_later_jobs_are_active(status):
    run, job = metadata()
    run["status"] = status
    assert marker_from_cycle_job(logs(), run=run, job=job, request_sha256="b"*64) == marker()


@pytest.mark.parametrize("scope,changes", [
    ("run", {"head_branch": "other"}), ("run", {"event": "pull_request"}),
    ("run", {"status": "completed", "conclusion": "failure"}),
    ("run", {"status": "completed", "conclusion": "cancelled"}),
    ("run", {"run_attempt": 3}), ("job", {"status": "in_progress", "conclusion": None}),
    ("job", {"status": "queued", "conclusion": None}),
    ("job", {"conclusion": "failure"}), ("job", {"conclusion": "cancelled"}),
    ("job", {"name": "chain"}), ("job", {"run_id": 67890}),
    ("job", {"run_attempt": 1}), ("job", {"head_sha": "f"*40}),
])
def test_fast_get_never_accepts_wrong_job_attempt_ref_or_failed_run(scope, changes):
    run, job = metadata()
    (run if scope == "run" else job).update(changes)
    with pytest.raises(ValueError):
        marker_from_cycle_job(logs(), run=run, job=job, request_sha256="b"*64)


@pytest.mark.parametrize("bad_logs", [
    json.dumps(marker()).encode(), logs().replace(b"state_committed", b"state_unknown"),
    logs()+logs(), logs({**marker(), "candidate_count": 30}),
    logs({**marker(), "workflow_run_attempt": 1}),
    logs({**marker(), "scheduler_run_id": True}),
])
def test_marker_requires_exact_unique_post_commit_identity(bad_logs):
    run, job = metadata()
    with pytest.raises(ValueError):
        marker_from_cycle_job(bad_logs, run=run, job=job, request_sha256="b"*64)


def test_marker_digest_is_exact_not_arbitrary_success_log():
    run, job = metadata()
    with pytest.raises(ValueError):
        marker_from_cycle_job(logs(), run=run, job=job, request_sha256="e"*64)


def test_marker_is_only_machine_metadata():
    value = copy.deepcopy(marker())
    value["body"] = "PRIVATE-NEVER-IN-LOGS"
    with pytest.raises(ValueError):
        validate_export_marker(value)
    assert hashlib.sha256(canonical_json(request()).encode()).hexdigest() != "b"*64


def test_downloaded_v1_must_match_digest_scheduler_and_exact_count():
    payload, receipt = request(), marker()
    receipt["gpt_fallback_request_sha256"] = hashlib.sha256(
        canonical_json(payload).encode()).hexdigest()
    validate_request_binding(receipt, payload)
    for changed in ({**payload, "scheduler_run_id": 18}, {**payload, "version": 2},
                    {**payload, "candidates": []}, {**payload, "body": "CHANGED"},
                    {**payload, "batch_request_sha256": "a"*64}):
        with pytest.raises(ValueError):
            validate_request_binding(receipt, changed)


@pytest.mark.parametrize("change", [
    {}, {"documents": []}, {"documents": [{"ownership": "private", "high_risk": False,
                                         "auto_publish": True}]},
    {"documents": [{"ownership": "official", "high_risk": False, "auto_publish": True},
                   {"ownership": "reader", "high_risk": True, "auto_publish": False}]},
    {"documents": [{"ownership": "official", "high_risk": 0, "auto_publish": True}]},
    {"documents": [{"ownership": "official", "high_risk": False, "auto_publish": True,
                    "source_type": "private"}]},
])
def test_fast_get_rejects_missing_or_private_documents_even_with_exact_hash(change):
    payload, receipt = request(), marker()
    payload["candidates"][0] = {"raw_item_id": 23, **change}
    receipt["gpt_fallback_request_sha256"] = hashlib.sha256(
        canonical_json(payload).encode()).hexdigest()
    with pytest.raises(ValueError):
        validate_request_binding(receipt, payload)


def export_database(tmp_path, monkeypatch, *, ownership="official",
                    source_type="official_city_news", secondary_ownership=None):
    """Настоящий builder и SQLite; только время и синтетический реестр фиксированы."""
    database = tmp_path / "public.sqlite3"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    Base.metadata.create_all(engine)
    registry = gpt_fallback._registry()
    with Session(engine) as session:
        raw, _ = seed_fallback(session)
        raw.body += " PRIVATE_READER_SENTINEL"
        source = session.get(Source, raw.source_id)
        source.ownership, source.source_type = ownership, source_type
        config = registry[source.id].model_copy(update={
            "ownership": ownership, "source_type": source_type})
        registry = {config.id: config}
        if secondary_ownership is not None:
            secondary = Source(id="synthetic_secondary", city=source.city, name="Другой источник",
                url=source.url, source_type=source_type, trust=source.trust,
                ownership=secondary_ownership, high_risk=source.high_risk,
                auto_publish=source.auto_publish, enabled=True)
            session.add(secondary)
            session.flush()
            session.add(RawItem(source_id=secondary.id, external_id="secondary", url=raw.url,
                title=raw.title, body=raw.body, published_at=raw.published_at,
                published_at_precision=raw.published_at_precision, ingested_at=raw.ingested_at))
            registry[secondary.id] = config.model_copy(update={
                "id": secondary.id, "ownership": secondary_ownership})
        session.commit()
        monkeypatch.setattr(gpt_fallback, "_registry", lambda: registry)
        built = gpt_fallback.build_request(session, now=NOW)
        assert built is not None  # Прежний builder сам по себе не ловит ошибочную регистрацию.
        assert any("PRIVATE_READER_SENTINEL" in doc["body"]
                   for candidate in built["candidates"] for doc in candidate["documents"])
        if secondary_ownership is not None:
            assert len(built["candidates"][0]["documents"]) == 2
    engine.dispose()
    monkeypatch.setattr(runtime, "datetime", SimpleNamespace(now=lambda *a: NOW))
    return database


@pytest.mark.parametrize("ownership,source_type,secondary", [
    ("private", "official_city_news", None),
    ("reader", "official_city_news", None),
    ("official", "private", None),
    ("official", "reader", None),
    ("official", "official_city_news", "private"),
    ("media", "news_media", "reader"),
])
def test_real_builder_never_uploads_private_or_reader_bundles(
    tmp_path, monkeypatch, capsys, ownership, source_type, secondary,
):
    database = export_database(tmp_path, monkeypatch, ownership=ownership,
        source_type=source_type, secondary_ownership=secondary)
    storage = PrivateStore({})
    with pytest.raises(ValueError):
        runtime._export_committed_fallback(storage, database, tmp_path,
            ready={"snapshot": {"sha256": "c"*64}, "generation": "d"*32},
            mode="editorial", environment=hosted_environment())
    captured = capsys.readouterr()
    assert storage.files == {} and not (tmp_path / "gpt-committed-request.json").exists()
    assert EXPORT_KIND not in captured.out
    assert "PRIVATE_READER_SENTINEL" not in captured.out+captured.err


@pytest.mark.parametrize("ownership,source_type", [
    ("official", "official_city_news"), ("media", "news_media"),
    ("official", "official_venue_events"), ("official", "official_guide"),
])
def test_registered_public_news_venue_and_guide_still_export(
    tmp_path, monkeypatch, capsys, ownership, source_type,
):
    database = export_database(tmp_path, monkeypatch, ownership=ownership, source_type=source_type)
    storage = PrivateStore({})
    runtime._export_committed_fallback(storage, database, tmp_path,
        ready={"snapshot": {"sha256": "c"*64}, "generation": "d"*32},
        mode="editorial", environment=hosted_environment())
    captured = capsys.readouterr()
    receipt = json.loads(captured.out)
    assert receipt["gpt_fallback_export"] == EXPORT_KIND
    payload = json.loads(storage.download(receipt["gpt_fallback_request_sha256"]))
    validate_request_binding(receipt, payload)
    assert "PRIVATE_READER_SENTINEL" not in captured.out+captured.err


@pytest.mark.parametrize("next_candidate", ["none", "same_cycle", "other_cycle", "boundary"])
def test_committed_export_selects_candidates_with_time_to_respond(
    tmp_path, monkeypatch, capsys, next_candidate,
):
    database = export_database(tmp_path, monkeypatch)
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with Session(engine) as session:
            first = session.scalar(select(RawItem).where(RawItem.external_id == "fallback-1"))
            cycle = session.scalar(select(SchedulerRun))
            preferred_id = cycle.id
            time_remaining = (timedelta(minutes=10) if next_candidate == "boundary"
                              else timedelta(seconds=64))
            fallback_opened = NOW - timedelta(minutes=30) + time_remaining
            assert adopt_fallback_request(session, first.id, now=fallback_opened)
            assert response_deadline(session, first.id) == NOW + time_remaining
            expected_id = first.id
            expected_cycle = preferred_id
            if next_candidate in {"same_cycle", "other_cycle"}:
                second = RawItem(
                    source_id=first.source_id, external_id="fallback-next",
                    url="https://www.beograd.rs/lat/beoinfo-vesti/a116900/Izlozba.html",
                    title="Izložba u Beogradu",
                    body="Izložba se otvara 18. novembra u 19 časova u Kulturnom centru.",
                    published_at=first.published_at, published_at_precision="instant",
                    ingested_at=first.ingested_at,
                )
                session.add(second)
                session.flush()
                errors = [{"raw_item_id": second.id, "error_type": "GeminiWorkerError"}]
                if next_candidate == "same_cycle":
                    cycle.details = {"processing_errors": [*cycle.details["processing_errors"],
                                                           *errors]}
                else:
                    second_cycle = SchedulerRun(
                        task_key=cycle.task_key, task_type=cycle.task_type, city=cycle.city,
                        status="partial", scheduled_for=NOW - timedelta(minutes=5),
                        started_at=NOW - timedelta(minutes=5),
                        finished_at=NOW - timedelta(minutes=3),
                        details={"processing_errors": errors},
                    )
                    session.add(second_cycle)
                    session.flush()
                    expected_cycle = second_cycle.id
                expected_id = second.id
            session.commit()
    finally:
        engine.dispose()
    cycle_directory = tmp_path / "cycle"
    cycle_directory.mkdir()
    (cycle_directory / "server-report.json").write_text(
        json.dumps({"status": "PRIMARY_PARTIAL", "run_id": preferred_id}), encoding="utf-8",
    )
    storage = PrivateStore({})
    runtime._export_committed_fallback(
        storage, database, tmp_path, ready={"snapshot": {"sha256": "c"*64}, "generation": "d"*32},
        mode="editorial", environment=hosted_environment(),
    )
    receipt = json.loads(capsys.readouterr().out)
    if next_candidate == "none":
        assert receipt == {"gpt_fallback_export_status": "no_pending_candidates"}
        assert storage.files == {}
        return
    payload = json.loads(storage.download(receipt["gpt_fallback_request_sha256"]))
    validate_request_binding(receipt, payload)
    assert payload["scheduler_run_id"] == expected_cycle
    assert [candidate["raw_item_id"] for candidate in payload["candidates"]] == [expected_id]
