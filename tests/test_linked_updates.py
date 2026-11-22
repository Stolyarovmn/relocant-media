import hashlib
import json
from datetime import timedelta

import pytest
from facts_check_helpers import bind_facts_check
from test_editorial_package import bind_context, event_payload, parse
from test_gpt_fallback_service import NOW, package_for_request
from test_gpt_fallback_service import fallback_session as _fallback_session

from relocant_media.ai.package import canonical_json, context_sha256, evaluate_package
from relocant_media.ops.gpt_export_receipt import require_public_documents
from relocant_media.services import gpt_fallback
from relocant_media.services.followup import revision_reply_target
from relocant_media.services.linked_updates import (
    published_post_reference,
    published_post_references,
    validate_linked_update,
)
from relocant_media.services.package_import import import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

fallback_session = _fallback_session


@pytest.fixture
def published(fallback_session):
    session, raw, scheduler = fallback_session
    request = gpt_fallback.build_request(session, now=NOW)
    package = package_for_request(request)
    run = import_package(session, package, now=NOW)
    post = session.get(Post, run.post_id)
    run.delivery_status = "sent"
    post.telegram_message_id = 150
    post.published_at = NOW
    session.commit()
    ref = published_post_reference(session, raw.id, city="belgrade", now=NOW)
    return session, raw, scheduler, run, post, package, ref


def linked_payload(package, reference):
    payload = package.model_dump(mode="json")
    payload["raw_item_id"] = 999
    payload["article_id"] = "new-results"
    payload["event"]["documents"][0]["raw_item_id"] = 999
    payload["linked_update"] = {"parent": reference.model_dump(mode="json"),
                                "change_claim_ids": list(package.outcome.draft.claim_ids)}
    for review in payload["reviews"]:
        review["article_id"] = "new-results"
    bind_facts_check(payload)
    for review in payload["reviews"]:
        if review["role"] == "facts":
            review["facts_check"]["linked_update_check"] = {
                "parent_message_id": reference.message_id,
                "parent_publication_checked": True, "same_event": True,
                "substantive_new_facts": True,
                "change_claim_ids": list(package.outcome.draft.claim_ids),
                "reason": "Синтетическое независимое ревью существенных новых фактов.",
            }
    bind_context(payload)
    return payload


def test_optional_context_preserves_historical_package_and_review_hashes():
    package = parse(event_payload())
    assert "linked_update" not in package.model_dump(mode="json")
    assert all("linked_update_check" not in review.facts_check.model_dump(mode="json")
               for review in package.reviews if review.facts_check)
    assert context_sha256(package) == (
        "476a2b4417e05426afe6843d8e7701baf01b5f4f70367943c31d03fa3722cd7d")
    assert hashlib.sha256(canonical_json(package.model_dump(mode="json")).encode()).hexdigest() == (
        "14e2880975c6b37623bf4a77cf31b5e647cc75fea582bcd15df88a8638c9b916")


def test_reference_exports_only_bounded_public_metadata_and_matching_receipt(published):
    session, raw, _, _, _, package, reference = published
    assert reference.message_id == 150 and reference.raw_item_id == raw.id
    refs = published_post_references(session, city="belgrade", now=NOW)
    assert refs == [reference.model_dump(mode="json")]
    assert "body" not in refs[0] and "reviews" not in refs[0]
    request = {"candidates": [{"documents": package.event.model_dump(mode="json")["documents"]}],
               "published_post_refs": refs}
    require_public_documents(request)
    validate_linked_update(session, parse(linked_payload(package, reference)), now=NOW)


@pytest.mark.parametrize("guard", ["pending", "sending", "uncertain", "foreign_city", "missing_id",
                                    "post_changed", "package_changed", "reader", "high_risk",
                                    "private_source", "expired", "test_prefix"])
def test_unconfirmed_private_changed_or_expired_parent_is_never_exported(published, guard):
    session, raw, _, run, post, _, _ = published
    source = session.get(Source, raw.source_id)
    now = NOW
    if guard in {"pending", "sending", "uncertain"}:
        run.delivery_status = guard
    elif guard == "foreign_city":
        post.city = "other"
    elif guard == "missing_id":
        post.telegram_message_id = None
    elif guard == "post_changed":
        post.body += " Изменение после исходного пакета."
    elif guard == "package_changed":
        run.outcome = {**run.outcome, "_package_sha256": "0" * 64}
    elif guard == "reader":
        run.outcome = {**run.outcome, "_reader_submission_id": 7}
    elif guard == "high_risk":
        source.high_risk = True
    elif guard == "private_source":
        source.ownership = "reader"
    elif guard == "expired":
        now += timedelta(hours=49)
    elif guard == "test_prefix":
        post.title = "[тестовый запуск] " + post.title
    session.commit()
    with pytest.raises(ValueError):
        published_post_reference(session, raw.id, city="belgrade", now=now)
    assert published_post_references(session, city="belgrade", now=now) == []


@pytest.mark.parametrize("field", [
    "message_id", "channel_id", "post_sha256", "package_sha256", "title",
])
def test_parent_context_is_revalidated_against_canonical_state(published, field):
    session, _, _, _, _, package, reference = published
    payload = linked_payload(package, reference)
    replacement = {"message_id": 151, "channel_id": -123, "post_sha256": "0" * 64,
                   "package_sha256": "0" * 64, "title": "Иной заголовок"}
    payload["linked_update"]["parent"][field] = replacement[field]
    bind_context(payload)
    with pytest.raises(ValueError):
        validate_linked_update(session, parse(payload), now=NOW)


@pytest.mark.parametrize("field", ["missing", "same_event", "substantive_new_facts",
                                    "parent_publication_checked", "parent_message_id",
                                    "change_claim_ids"])
def test_link_cannot_approve_without_an_exact_independent_relation_review(published, field):
    _, _, _, _, _, package, reference = published
    payload = linked_payload(package, reference)
    check = payload["reviews"][0]["facts_check"]["linked_update_check"]
    if field == "missing":
        del payload["reviews"][0]["facts_check"]["linked_update_check"]
    elif field == "parent_message_id":
        check[field] = 151
    elif field == "change_claim_ids":
        check[field] = ["unverified"]
    else:
        check[field] = False
    bind_context(payload)
    result = evaluate_package(parse(payload), now=NOW)
    assert result.status == "human_review" and result.reason == "linked_update_review_required"


def test_valid_link_uses_existing_reply_route_and_blocks_a_changed_parent(published):
    session, _, _, _, post, package, reference = published
    payload = linked_payload(package, reference)
    child = RawItem(id=999, source_id=package.source_id, external_id="new-results",
                    url=package.evidence.url + "?new=1",
                    title="Новое изменение", body="Новые факты")
    session.add(child)
    session.flush()
    session.add(EditorialRun(raw_item_id=999, evidence={}, outcome={"_package": payload},
                             delivery_status="pending"))
    session.commit()
    assert evaluate_package(parse(payload), now=NOW).status == "ready"
    assert revision_reply_target(session, 999, "belgrade", now=NOW) == 150
    post.body += " Изменённая версия."
    session.commit()
    with pytest.raises(ValueError):
        revision_reply_target(session, 999, "belgrade", now=NOW)


def test_linked_update_does_not_bypass_existing_event_dedup(published):
    session, raw, _, _, _, package, reference = published
    payload = linked_payload(package, reference)
    child = RawItem(id=999, source_id=raw.source_id, external_id="new-results",
                    url=raw.url + "?copy=1",
                    title=raw.title, body=raw.body, published_at=raw.published_at,
                    published_at_precision=raw.published_at_precision, ingested_at=raw.ingested_at)
    session.add(child)
    session.commit()
    parent = payload["linked_update"]["parent"]
    check = payload["reviews"][0]["facts_check"]["linked_update_check"]
    payload = json.loads(json.dumps(payload).replace(raw.url, child.url))
    payload["linked_update"]["parent"] = parent
    bind_facts_check(payload)
    payload["reviews"][0]["facts_check"]["linked_update_check"] = check
    bind_context(payload)
    from relocant_media.domain.dedup import event_key
    from relocant_media.services.editorial import _candidate
    payload["event"]["event_key"] = event_key(_candidate(child))
    bind_context(payload)
    with pytest.raises(ValueError, match="event already reviewed"):
        import_package(session, parse(payload), now=NOW)
    assert session.get(EditorialRun, child.id) is None


@pytest.mark.parametrize("private_field", ["body", "reviews", "unknown"])
def test_export_boundary_rejects_extra_parent_payload(published, private_field):
    _, _, _, _, _, package, reference = published
    value = reference.model_dump(mode="json")
    value[private_field] = "ЗАКРЫТЫЙ ТЕКСТ"
    request = {"candidates": [{"documents": package.event.model_dump(mode="json")["documents"]}],
               "published_post_refs": [value]}
    with pytest.raises(ValueError):
        require_public_documents(request)


def test_fallback_exports_reference_read_only_and_only_with_real_candidates(published):
    from sqlalchemy import event

    session, raw, scheduler, _, _, _, reference = published
    child = RawItem(source_id=raw.source_id, external_id="different-exhibition",
                    url="https://www.beograd.rs/lat/beoinfo-vesti/a9999/izlozba.html",
                    title="Izlozba u muzeju na Dorcolu", body="Nova izlozba 13. novembra u muzeju.",
                    published_at=NOW, published_at_precision="instant", ingested_at=NOW)
    session.add(child)
    session.flush()
    scheduler.details = {"processing_errors": [{"raw_item_id": child.id,
                                              "error_type": "GeminiWorkerError"}]}
    session.commit()
    statements = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())

    engine = session.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        request = gpt_fallback.build_request(session, now=NOW)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert request is not None and request["published_post_refs"] == [
        reference.model_dump(mode="json")]
    assert set(statements) <= {"SELECT", "PRAGMA"}
    require_public_documents(request)


def test_self_reference_is_rejected(published):
    session, _, _, _, _, package, reference = published
    payload = linked_payload(package, reference)
    payload["raw_item_id"] = reference.raw_item_id
    payload["event"]["documents"][0]["raw_item_id"] = reference.raw_item_id
    bind_context(payload)
    with pytest.raises(ValueError, match="собственным"):
        validate_linked_update(session, parse(payload), now=NOW)


def test_import_stores_verified_link_and_exact_sent_reimport_keeps_receipt(published):
    from facts_check_helpers import reviewed_package_for

    session, raw, _, _, parent_post, _, reference = published
    source = session.get(Source, raw.source_id)
    child = RawItem(source_id=raw.source_id, external_id="changed-water-hours",
                    url="https://www.beograd.rs/lat/beoinfo-vesti/a9998/promena.html",
                    title="Izmenjeni termini radova na Zvezdari",
                    body="Radovi na vodovodnoj mrezi u Zvezdari trajace od 9 do 18 casova.",
                    published_at=NOW, published_at_precision="instant", ingested_at=NOW)
    session.add(child)
    session.flush()
    payload = reviewed_package_for(child, source, now=NOW,
        title="Изменились часы работ на Звездаре",
        body="Работы на водопроводной сети на Звездаре продлятся с 9 до 18 часов.")
    payload["outcome"]["ranking"]["category"] = "utilities"
    ids = payload["outcome"]["draft"]["claim_ids"]
    payload["linked_update"] = {"parent": reference.model_dump(mode="json"),
                                "change_claim_ids": ids}
    payload["reviews"][0]["facts_check"]["linked_update_check"] = {
        "parent_message_id": 150, "parent_publication_checked": True, "same_event": True,
        "substantive_new_facts": True, "change_claim_ids": ids,
        "reason": "Синтетическая независимая проверка новых часов того же события.",
    }
    bind_context(payload)
    package = parse(payload)
    run = import_package(session, package, now=NOW)
    assert run.outcome["_package"]["linked_update"] == payload["linked_update"]
    assert revision_reply_target(session, child.id, "belgrade", now=NOW) == 150
    # Моделируем прежнюю реальную квитанцию, не вызывая Telegram из теста.
    post = session.get(Post, run.post_id)
    run.delivery_status = "sent"
    post.telegram_message_id = 151
    post.published_at = NOW
    parent_post.body += " Изменённая исходная публикация."
    session.commit()
    saved = import_package(session, package, now=NOW + timedelta(days=3))
    assert saved.delivery_status == "sent"
    assert session.get(Post, saved.post_id).telegram_message_id == 151


def test_fallback_package_cannot_choose_a_parent_outside_its_request(published, monkeypatch):
    from types import SimpleNamespace

    from relocant_media.domain.city_profile import load_city_profile

    session, _, _, _, _, package, reference = published
    candidate = parse(linked_payload(package, reference))
    expected = {"source_id": candidate.source_id, "article_id": candidate.article_id,
                "event_key": candidate.event.event_key,
                "documents": [doc.model_dump(mode="json") for doc in candidate.event.documents]}
    monkeypatch.setattr(gpt_fallback, "validate_fallback_candidate", lambda *args, **kwargs: (
        SimpleNamespace(published_post_refs=[]), load_city_profile("belgrade"),
        SimpleNamespace(flight_review_parent=None), expected,
    ))
    with pytest.raises(gpt_fallback.FallbackValidationError, match="каноническом запросе"):
        gpt_fallback.validate_fallback_package(session, {}, candidate, now=NOW)


def test_export_boundary_rejects_duplicate_parent_references(published):
    _, _, _, _, _, package, reference = published
    value = reference.model_dump(mode="json")
    request = {"candidates": [{"documents": package.event.model_dump(mode="json")["documents"]}],
               "published_post_refs": [value, value]}
    with pytest.raises(ValueError, match="Повтор"):
        require_public_documents(request)


def test_private_secondary_document_disqualifies_entire_parent(published):
    session, raw, _, run, _, package, _ = published
    source = Source(id="private_reader", city="belgrade", name="Закрытая предложка",
                    url="https://reader.example/", source_type="reader_intake", ownership="reader",
                    enabled=True, high_risk=False, auto_publish=False, trust=0.5)
    session.add(source)
    session.flush()
    secondary = RawItem(source_id=source.id, external_id="private", url="https://reader.example/a",
                        title="Приватное обращение", body="ПРИВАТНЫЙ ТЕКСТ", published_at=NOW,
                        published_at_precision="instant", ingested_at=NOW)
    session.add(secondary)
    session.flush()
    payload = package.model_dump(mode="json")
    payload["event"]["documents"].append({
        "raw_item_id": secondary.id, "source_id": source.id, "url": secondary.url,
        "title": secondary.title, "body": secondary.body, "published_at": NOW.isoformat(),
        "published_at_precision": "instant", "ownership": "reader", "high_risk": False,
        "auto_publish": False,
    })
    bind_context(payload)
    parsed = parse(payload)
    run.outcome = {**run.outcome, "_package": parsed.model_dump(mode="json"),
                   "_package_sha256": hashlib.sha256(canonical_json(
                       parsed.model_dump(mode="json")).encode()).hexdigest()}
    session.commit()
    with pytest.raises(ValueError):
        published_post_reference(session, raw.id, city="belgrade", now=NOW)
    assert published_post_references(session, city="belgrade", now=NOW) == []


def test_explicit_link_cannot_override_existing_source_revision_lineage(published):
    from relocant_media.storage.models import SourceRevision
    from relocant_media.storage.revision_control import SourceRevisionMaterialization

    session, raw, _, _, _, package, reference = published
    payload = linked_payload(package, reference)
    child = RawItem(id=999, source_id=raw.source_id, external_id="revision-child",
                    url=raw.url + "?revision=1", title="Новые часы", body="Новая версия")
    session.add(child)
    revision = SourceRevision(raw_item_id=raw.id, snapshot_sha256="c" * 64,
                              evidence={}, observed_at=NOW)
    session.add(revision)
    session.flush()
    session.add(SourceRevisionMaterialization(revision_id=revision.id, raw_item_id=child.id,
                                             parent_raw_item_id=raw.id, created_at=NOW))
    session.commit()
    with pytest.raises(ValueError, match="подменить"):
        validate_linked_update(session, parse(payload), now=NOW)
    # Существующая source-revision связь продолжает давать свой прежний reply target.
    session.add(EditorialRun(raw_item_id=child.id, evidence={}, outcome={},
                             delivery_status="pending"))
    session.commit()
    assert revision_reply_target(session, child.id, "belgrade", now=NOW) == 150


def test_runtime_export_rechecks_parent_from_canonical_database(published, monkeypatch):
    from relocant_media.ops import actions_runtime

    session, _, _, _, post, package, reference = published
    request = {"candidates": [{"documents": package.event.model_dump(mode="json")["documents"]}],
               "published_post_refs": [reference.model_dump(mode="json")]}

    class Clock:
        @staticmethod
        def now(*args, **kwargs):
            return NOW

    monkeypatch.setattr(actions_runtime, "datetime", Clock)
    actions_runtime._require_public_fallback_documents(session, request)
    post.body += " Изменённый текст после сохранённого контекста."
    session.commit()
    with pytest.raises(ValueError):
        actions_runtime._require_public_fallback_documents(session, request)
