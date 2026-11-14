import hashlib
import json
import sys
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_gpt_fallback_service import NOW, package_for_request
from test_gpt_fallback_service import fallback_session as service_session
from test_server_cycle import seeded as server_seeded

from relocant_media.ai.models import EditorialOutcome, EvidenceDocument
from relocant_media.ai.package import EditorialPackage, canonical_json, context_sha256, draft_sha256
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.dedup import event_key
from relocant_media.domain.presentation import presentation_title
from relocant_media.ops import gpt_fallback_worker as worker
from relocant_media.services import gpt_fallback
from relocant_media.services.editorial import _candidate, _snapshot_member
from relocant_media.services.editorial_products import build_presentation, present_draft
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

seeded = server_seeded
fallback_session = service_session


def _input(package, request=None):
    request = request or {"version": 1, "private": "PRIVATE-REQUEST-SENTINEL"}
    return {
        "request": request,
        "result": {"version": 1,
                   "request_sha256": hashlib.sha256(canonical_json(request).encode()).hexdigest(),
                   "packages": [package.model_dump(mode="json")]},
    }


def _parse(value):
    return worker.FallbackInput.model_validate_json(json.dumps(value))


@pytest.fixture
def ready_package(seeded):
    engine, raw_id, provider = seeded
    profile = load_city_profile("belgrade")
    with Session(engine) as session:
        raw = session.get(RawItem, raw_id)
        source = session.get(Source, raw.source_id)
        source.auto_publish = True
        session.commit()
        evidence = _snapshot_member(raw, source)
        document = EvidenceDocument.model_validate(evidence)
        presentation = build_presentation(
            session, raw_id, (document,), document_ids=[raw_id],
            profile=profile, rubric_id="city_changes",
        )
        draft = provider._responses["draft"].model_copy(update={
            "title": presentation_title(provider._responses["draft"].title),
        })
        draft = present_draft(draft, presentation, category=provider._responses["rank"].category)
        outcome = EditorialOutcome(
            status="ready", reason="verified", confidence=.99,
            ranking=provider._responses["rank"], claims=provider._responses["claims"],
            verification=provider._responses["verify"], draft=draft,
        )
        values = {
            "version": 2, "policy_version": "subscription-editorial-v2", "city": source.city,
            "raw_item_id": raw_id, "source_id": raw.source_id, "article_id": raw.external_id,
            "evidence": document.model_dump(mode="json"),
            "event": {"event_key": event_key(_candidate(raw)),
                      "editorial_rule_version": "belgrade-editorial-v2", "documents": [evidence]},
            "presentation": presentation.model_dump(mode="json"),
            "outcome": outcome.model_dump(mode="json"),
            "reviews": [{"reviewer_id": f"subscription-gpt-{role}", "role": role,
                         "source_id": raw.source_id, "article_id": raw.external_id,
                         "reviewed_at": datetime.now(UTC).isoformat(), "verdict": "approve",
                         "score": 94, "flags": [], "live_source_checked": True,
                         "draft_sha256": draft_sha256(draft), "context_sha256": "0" * 64}
                        for role in ("facts", "utility")],
        }
    package = EditorialPackage.model_validate_json(json.dumps(values))
    digest = context_sha256(package)
    package = package.model_copy(update={"reviews": tuple(
        review.model_copy(update={"context_sha256": digest}) for review in package.reviews
    )})
    return engine, package


@pytest.fixture
def offline(monkeypatch):
    calls = []

    def validate(session, request, package, *, now):
        calls.append("validate")
        assert now.tzinfo is not None
        return package

    def live(package):
        calls.append("live")
        return tuple(EvidenceDocument.model_validate(
            document.model_dump(include=set(EvidenceDocument.model_fields)))
            for document in package.event.documents)

    def forbidden(*args, **kwargs):
        raise AssertionError("ИИ, intake и обратная связь не входят в GPT fallback")

    monkeypatch.setattr(worker, "validate_fallback_package", validate)
    monkeypatch.setattr(worker.server_runtime, "fetch_documents", live)
    monkeypatch.setattr(worker.server_cycle, "IsolatedGeminiProvider", forbidden)
    monkeypatch.setattr(worker.server_cycle, "receive_private_tips", forbidden)
    monkeypatch.setattr(worker.server_cycle, "reply_to_submissions", forbidden)
    return calls


@pytest.mark.parametrize("mutation", ["hash", "duplicate", "empty", "version", "unknown"])
def test_input_contract_is_strict_and_binds_the_request(ready_package, mutation):
    _, package = ready_package
    payload = _input(package)
    if mutation == "hash":
        payload["result"]["request_sha256"] = "0" * 64
    elif mutation == "duplicate":
        payload["result"]["packages"] *= 2
    elif mutation == "empty":
        payload["result"]["packages"] = []
    elif mutation == "version":
        payload["result"]["version"] = True
    else:
        payload["result"]["skip_validation"] = True
    with pytest.raises(ValidationError):
        _parse(payload)


def test_worker_revalidates_live_evidence_then_uses_normal_import(ready_package, offline):
    engine, package = ready_package
    result = worker.run_worker(engine, _parse(_input(package)))
    assert offline == ["validate", "live", "validate"]
    assert result["status"] == "PRIMARY_OK"
    assert result["imported"] == result["ready"] == 1
    assert result["sent_count"] == result["model_calls"] == 0
    with Session(engine) as session:
        run = session.get(EditorialRun, package.raw_item_id)
        assert run.outcome["_package"] == package.model_dump(mode="json")
        assert run.delivery_status == "pending"
        assert session.get(Post, run.post_id).body == package.outcome.draft.body
    assert "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("mutation", ["changed", "missing", "map_error"])
def test_changed_live_evidence_or_map_never_reaches_import(
    ready_package, offline, monkeypatch, mutation,
):
    engine, package = ready_package

    def live(package):
        if mutation == "map_error":
            raise ValueError("PRIVATE-MAP-URL")
        if mutation == "missing":
            return ()
        return (package.evidence.model_copy(update={"body": "Изменённый текст"}),)

    monkeypatch.setattr(worker.server_runtime, "fetch_documents", live)
    result = worker.run_worker(engine, _parse(_input(package)), send_enabled=True)
    assert offline == ["validate"]
    assert result["status"] == "PRIMARY_FAILED"
    assert result["processing_errors"] == [{"stage": "live", "error_type": "ValueError"}]
    assert "PRIVATE" not in json.dumps(result)
    with Session(engine) as session:
        assert session.get(EditorialRun, package.raw_item_id) is None
        assert session.scalar(select(Post)) is None


def test_revalidation_failure_prevents_import_after_successful_live_fetch(
    ready_package, offline, monkeypatch,
):
    engine, package = ready_package
    validated = []

    def validate(session, request, package, *, now):
        validated.append(True)
        if len(validated) == 2:
            raise ValueError("PRIVATE-CHANGED-SCHEDULER")
        return package

    monkeypatch.setattr(worker, "validate_fallback_package", validate)
    result = worker.run_worker(engine, _parse(_input(package)))
    assert validated == [True, True]
    assert result["processing_errors"] == [{"stage": "revalidate", "error_type": "ValueError"}]
    with Session(engine) as session:
        assert session.get(EditorialRun, package.raw_item_id) is None


@pytest.mark.parametrize("invalidate", [False, True])
def test_real_fallback_validation_requires_the_recorded_gemini_failure(
    fallback_session, monkeypatch, invalidate,
):
    session, raw, scheduler = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    fetched = []
    if invalidate:
        scheduler.details = {"processing_errors": [
            {"raw_item_id": raw.id, "error_type": "ValueError"},
        ]}
        session.commit()

    def live(value):
        fetched.append(True)
        return tuple(EvidenceDocument.model_validate(document.model_dump())
                     for document in value.event.documents)

    monkeypatch.setattr(worker.server_runtime, "fetch_documents", live)
    result = worker.run_worker(session.get_bind(), _parse(_input(package, request)), now=NOW)
    assert result["imported"] == (0 if invalidate else 1)
    assert result["fallback_required"] is invalidate
    assert fetched == ([] if invalidate else [True])
    assert (session.get(EditorialRun, raw.id) is None) is invalidate


def test_real_gemini_failure_can_recover_an_existing_selection_hold(
    fallback_session, monkeypatch,
):
    session, raw, _ = fallback_session
    source = session.get(Source, raw.source_id)
    document = EvidenceDocument.model_validate(_snapshot_member(raw, source))
    hold = EditorialRun(
        raw_item_id=raw.id, delivery_status="pending",
        outcome={"status": "human_review", "reason": "selection_ungrounded_evidence"},
        evidence={**document.model_dump(mode="json"), "city": source.city,
                  "event_key": event_key(_candidate(raw)),
                  "documents": [_snapshot_member(raw, source)]},
    )
    session.add(hold)
    session.commit()
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == {raw.id}
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    monkeypatch.setattr(worker, "_check_live_evidence", lambda package: None)
    result = worker.run_worker(session.get_bind(), _parse(_input(package, request)), now=NOW)
    assert result["imported"] == 1
    assert result["unresolved"] == 0
    session.refresh(hold)
    assert hold.outcome["_previous_policy_hold"]["outcome"]["reason"] == (
        "selection_ungrounded_evidence"
    )
    assert hold.outcome["_autonomous_assessment"]["reviewed_package"]
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == set()


def test_gpt_fallback_does_not_reopen_a_hold_with_a_delivery_receipt(fallback_session):
    session, raw, _ = fallback_session
    hold = EditorialRun(
        raw_item_id=raw.id, delivery_status="pending", staging_message_id=42,
        outcome={"status": "human_review", "reason": "selection_ungrounded_evidence"},
        evidence={},
    )
    session.add(hold)
    session.commit()
    assert gpt_fallback.pending_gpt_ids(session, now=NOW) == set()
    assert gpt_fallback.build_request(session, now=NOW) is None


@pytest.mark.parametrize("existing_hold", [False, True])
def test_subset_response_keeps_unresolved_tail_but_accepts_a_known_human_hold(
    ready_package, offline, existing_hold,
):
    engine, package = ready_package
    with Session(engine) as session:
        other = RawItem(
            source_id=package.source_id, external_id="unanswered", url="https://example.com/other",
            title="Самостоятельная тема", body="Другой материал из запроса", published_at=None,
        )
        session.add(other)
        session.flush()
        other_id = other.id
        if existing_hold:
            session.add(EditorialRun(raw_item_id=other_id, evidence={},
                                     outcome={"status": "human_review"}, delivery_status="pending"))
        session.commit()
    request = {"candidates": [{"raw_item_id": package.raw_item_id}, {"raw_item_id": other_id}]}
    result = worker.run_worker(engine, _parse(_input(package, request)))
    assert result["unresolved"] == (0 if existing_hold else 1)
    assert result["fallback_required"] is not existing_hold
    assert result["processing_errors"] == []


@pytest.mark.parametrize("kind", ["source_map", "native_map"])
def test_government_map_fails_closed_until_its_live_adapter_checks_html(
    ready_package, offline, kind,
):
    _, package = ready_package
    document = package.event.documents[0].model_copy(update={
        "source_id": worker.server_runtime.SOURCE_ID,
    })
    additions = ({"source_map": {"source_page": document.url}}
                 if kind == "source_map" else {"native_map": {"point": {
                     "source_url": document.url}}})
    package = package.model_copy(update={
        "event": package.event.model_copy(update={"documents": (document,)}),
        "presentation": package.presentation.model_copy(update=additions),
    })
    with pytest.raises(ValueError, match="живую проверку карты"):
        worker._check_live_evidence(package)
    assert offline == []


def test_only_successfully_imported_packages_enter_normal_delivery(
    ready_package, offline, monkeypatch,
):
    engine, package = ready_package
    payload = _input(package)
    other = package.model_dump(mode="json")
    other["raw_item_id"] = package.raw_item_id + 100
    other["event"]["documents"][0]["raw_item_id"] = other["raw_item_id"]
    payload["result"]["packages"].append(other)
    sent = []
    original_fetch = worker.server_cycle.fetch_documents

    def validate(session, request, value, *, now):
        if value.raw_item_id != package.raw_item_id:
            raise ValueError("PRIVATE-UNREQUESTED-CANDIDATE")
        return value

    async def publish(engine, *, enabled, only_raw_ids, blocked):
        assert enabled is True
        assert worker.server_cycle.fetch_documents is worker.server_runtime.fetch_documents
        sent.append(only_raw_ids)
        return [42]

    monkeypatch.setattr(worker, "validate_fallback_package", validate)
    monkeypatch.setattr(worker.server_cycle, "publish_pending", publish)
    result = worker.run_worker(engine, _parse(payload), send_enabled=True)
    assert result["status"] == "PRIMARY_PARTIAL"
    assert result["imported"] == result["rejected"] == result["sent_count"] == 1
    assert sent == [{package.raw_item_id}]
    assert worker.server_cycle.fetch_documents is original_fetch
    assert "PRIVATE" not in json.dumps(result)


def test_exact_sent_replay_preserves_receipt_without_entering_delivery(
    ready_package, offline, monkeypatch,
):
    engine, package = ready_package
    payload = _parse(_input(package))
    assert worker.run_worker(engine, payload)["imported"] == 1
    with Session(engine) as session:
        run = session.get(EditorialRun, package.raw_item_id)
        run.delivery_status = "sent"
        post = session.get(Post, run.post_id)
        post.telegram_message_id = 42
        post.published_at = datetime.now(UTC)
        session.commit()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Подтверждённая квитанция не должна запускать повторную отправку")

    monkeypatch.setattr(worker.server_cycle, "publish_pending", forbidden)
    result = worker.run_worker(engine, payload, send_enabled=True)
    assert result["status"] == "PRIMARY_OK" and result["sent_count"] == 0
    with Session(engine) as session:
        run = session.get(EditorialRun, package.raw_item_id)
        assert run.delivery_status == "sent"
        assert session.get(Post, run.post_id).telegram_message_id == 42


def test_delivery_failure_restores_fetch_adapter_and_does_not_retry(
    ready_package, offline, monkeypatch,
):
    engine, package = ready_package
    previous = worker.server_cycle.fetch_documents
    attempts = []

    async def failed(*args, **kwargs):
        attempts.append(True)
        raise TimeoutError("PRIVATE-SEND-DETAILS")

    monkeypatch.setattr(worker.server_cycle, "publish_pending", failed)
    result = worker.run_worker(engine, _parse(_input(package)), send_enabled=True)
    assert attempts == [True]
    assert result["status"] == "PRIMARY_PARTIAL"
    assert result["processing_errors"] == [{"stage": "publish", "error_type": "TimeoutError"}]
    assert worker.server_cycle.fetch_documents is previous
    assert "PRIVATE" not in json.dumps(result)


def test_cli_uses_existing_database_and_writes_only_structural_report(
    ready_package, offline, tmp_path, monkeypatch, capsys,
):
    engine, package = ready_package
    private = tmp_path / "private.json"
    private.write_text(json.dumps(_input(package)), encoding="utf-8")
    output = tmp_path / "new-cycle"
    monkeypatch.setenv("DATABASE_URL", str(engine.url))
    monkeypatch.delenv("EDITORIAL_SEND_ENABLED", raising=False)
    monkeypatch.setattr(sys, "argv", ["worker", "--input", str(private), "--output", str(output)])

    worker.main()

    printed = capsys.readouterr().out
    report = json.loads((output / "server-report.json").read_text(encoding="utf-8"))
    assert json.loads(printed) == report
    assert report["status"] == "PRIMARY_OK" and report["delivery_enabled"] is False
    assert "PRIVATE" not in printed and package.evidence.url not in printed
    assert package.outcome.draft.body not in printed
    assert [item.name for item in output.iterdir()] == ["server-report.json"]
    before = (output / "server-report.json").read_bytes()
    with pytest.raises(SystemExit):
        worker.main()
    assert (output / "server-report.json").read_bytes() == before


def test_invalid_input_does_not_open_database_or_echo_private_data(tmp_path, monkeypatch, capsys):
    private = tmp_path / "private.json"
    private.write_text('{"PRIVATE-INVALID": "private-input"}', encoding="utf-8")
    output = tmp_path / "new-cycle"
    monkeypatch.setattr(sys, "argv", ["worker", "--input", str(private), "--output", str(output)])
    monkeypatch.setattr(worker, "create_engine", lambda *args: pytest.fail("no database needed"))
    with pytest.raises(SystemExit):
        worker.main()
    printed = capsys.readouterr().out
    assert json.loads(printed)["error_type"] == "ValidationError"
    assert "PRIVATE" not in printed and "private-input" not in printed
