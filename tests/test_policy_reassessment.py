import copy

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from test_editorial_package import NOW, bind_context, event_payload, parse

from relocant_media.ai.models import EditorialOutcome
from relocant_media.domain.dedup import event_key
from relocant_media.services.editorial import _candidate, _document, _snapshot_member
from relocant_media.services.package_import import PackageConflictError, import_package
from relocant_media.services.policy_reassessment import (
    eligible_empty_policy_hold,
    persist_policy_reassessment,
)
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source


@pytest.fixture
def state():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        payload = event_payload()
        evidence = payload["evidence"]
        source = Source(id=payload["source_id"], city="belgrade", name="Город",
            url="https://example.test", source_type="official", trust=1,
            ownership="official", auto_publish=False, high_risk=False, enabled=True)
        session.add(source)
        session.flush()
        item = RawItem(id=1, source_id=source.id, external_id=payload["article_id"],
            **{k: evidence[k] for k in ("url", "title", "body")}, published_at=NOW)
        session.add(item)
        session.flush()
        doc = _document(item)
        snapshot = _snapshot_member(item, source)
        run = EditorialRun(raw_item_id=1, created_at=NOW,
            outcome={"status": "human_review", "reason": "source_auto_publish_disabled",
                     "claims": None, "draft": None, "verification": None},
            evidence={**doc.model_dump(mode="json"), "city": "belgrade",
                      "event_key": event_key(_candidate(item)), "documents": [snapshot]},
            delivery_status="pending")
        session.add(run)
        session.commit()
        payload["evidence"] = doc.model_dump(mode="json")
        payload["event"].update(event_key=event_key(_candidate(item)), documents=[snapshot])
        bind_context(payload)
        yield session, parse(payload), run, (doc,)
    engine.dispose()


def test_replace_is_explicit_audited_and_preserves_source_gate_and_creation(state):
    session, package, run, _ = state
    previous = copy.deepcopy(run.outcome)
    assert eligible_empty_policy_hold(session, 1, now=NOW)
    with pytest.raises(PackageConflictError):
        import_package(session, package, now=NOW)
    result = import_package(session, package, now=NOW, replace_empty_hold=True)
    assert result.created_at == NOW
    assert result.outcome["reason"] == "source_auto_publish_disabled"
    assert result.outcome["status"] == "human_review"
    assert result.outcome["_previous_policy_hold"]["outcome"] == previous
    assert session.scalar(select(func.count()).select_from(EditorialRun)) == 1
    assert session.scalar(select(func.count()).select_from(Post)) == 1
    assert not eligible_empty_policy_hold(session, 1, now=NOW)


@pytest.mark.parametrize("change", ["receipt", "sending", "draft", "package", "evidence", "old"])
def test_protected_holds_cannot_be_replaced(state, change):
    from datetime import timedelta
    session, package, run, _ = state
    now = NOW
    if change == "receipt":
        run.staging_message_id = 17
    elif change == "sending":
        run.delivery_status = "sending"
    elif change == "draft":
        run.outcome = {**run.outcome, "draft": {"title": "Защищённый"}}
    elif change == "package":
        run.outcome = {**run.outcome, "_package": {}}
    elif change == "evidence":
        run.evidence = {**run.evidence, "event_key": "b" * 64}
    else:
        now += timedelta(days=8)
    session.commit()
    assert not eligible_empty_policy_hold(session, 1, now=now)
    # Time-expired reviews also fail closed before any replacement.
    with pytest.raises(ValueError):
        import_package(session, package, now=now, replace_empty_hold=True)
    assert session.scalar(select(func.count()).select_from(Post)) == 0


@pytest.mark.parametrize("status", ["human_review", "skipped", "blocked"])
def test_nonready_assessment_closes_hold_without_post_or_repeat(state, status):
    session, package, run, documents = state
    outcome = EditorialOutcome.model_validate({**package.outcome.model_dump(),
        "status": status, "reason": "Автономная оценка"})
    result = persist_policy_reassessment(session, 1, outcome, documents, now=NOW)
    assert result.outcome["status"] == status
    assert result.outcome["_previous_policy_hold"]["outcome"]["draft"] is None
    assert not eligible_empty_policy_hold(session, 1, now=NOW)
    assert session.scalar(select(func.count()).select_from(Post)) == 0
    assert run.created_at == NOW


def test_ready_or_changed_evidence_cannot_bypass_reviewed_import(state):
    session, package, run, documents = state
    outcome = EditorialOutcome.model_validate(package.outcome.model_dump())
    with pytest.raises(ValueError, match="requires independently"):
        persist_policy_reassessment(session, 1, outcome, documents, now=NOW)
    outcome.status = "skipped"
    with pytest.raises(ValueError, match="evidence changed"):
        persist_policy_reassessment(session, 1, outcome,
            (documents[0].model_copy(update={"body": "Изменено"}),), now=NOW)
    assert run.outcome["reason"] == "source_auto_publish_disabled"
