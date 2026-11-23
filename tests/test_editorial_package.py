import json
from datetime import UTC, datetime, timedelta

import pytest
from facts_check_helpers import bind_facts_check
from pydantic import ValidationError
from test_editorial_pipeline import _claims, _document, _draft, _ranking, _verification

from relocant_media.ai.package import (
    EditorialPackage,
    context_sha256,
    draft_sha256,
    evaluate_package,
)

NOW = datetime(2026, 10, 19, 8, tzinfo=UTC)


@pytest.mark.parametrize("body", [
    "Крајњи рок за завршетак изградње моста је пролеће 2027. године.",
    "С 23 по 25 октября пройдёт такмичење у препонском јахању.",
])
def test_two_perfect_reviews_cannot_approve_untranslated_copy(body):
    payload = package_payload()
    payload["outcome"]["draft"]["body"] = body
    from relocant_media.ai.models import RussianDraft

    draft = RussianDraft.model_validate(payload["outcome"]["draft"])
    for review in payload["reviews"]:
        review.update(draft_sha256=draft_sha256(draft), score=100)
    bind_context(payload)
    result = evaluate_package(parse(payload), now=NOW)
    assert result.status == "human_review"
    assert result.reason == "draft_requires_russian_translation"


def package_payload() -> dict:
    payload = {
        "version": 1, "policy_version": "subscription-editorial-v1", "city": "belgrade",
        "raw_item_id": 1, "source_id": _document().source_id, "article_id": "editorial-test",
        "evidence": _document().model_dump(mode="json"),
        "outcome": {
            "status": "ready", "reason": "reviewed", "confidence": 0.99,
            "ranking": _ranking("transport").model_dump(mode="json"),
            "claims": _claims().model_dump(mode="json"),
            "verification": _verification().model_dump(mode="json"),
            "draft": _draft().model_dump(mode="json"),
        },
        "reviews": [{
            "reviewer_id": f"agent-{role}", "role": role,
            "source_id": _document().source_id, "article_id": "editorial-test",
            "draft_sha256": draft_sha256(_draft()), "reviewed_at": NOW.isoformat(),
            "context_sha256": "0" * 64,
            "verdict": "approve", "score": 80, "flags": [], "live_source_checked": True,
        } for role in ("facts", "utility")],
    }
    bind_facts_check(payload)
    bind_context(payload)
    return payload


def bind_context(payload: dict) -> None:
    digest = context_sha256(parse(payload))
    for review in payload["reviews"]:
        review["context_sha256"] = digest


def parse(payload: dict) -> EditorialPackage:
    return EditorialPackage.model_validate_json(json.dumps(payload))


def test_ready_requires_valid_independent_reviews() -> None:
    assert evaluate_package(parse(package_payload()), now=NOW).status == "ready"


@pytest.mark.parametrize(("source_title", "body", "reason"), [
    (
        "Kvarovi na vodovodnoj mreži biće otklonjeni tokom dana",
        "Когда: 18 ноября до 15:00. Автоцистерна приедет на улицу Пробную.",
        "water_tanker_scope_unverified",
    ),
    (
        "Kvarovi na vodovodnoj mreži biće otklonjeni tokom dana",
        "Когда: до 15:00. Работы пройдут на улице Пробной.",
        "draft_requires_absolute_event_date",
    ),
    (
        "Bez vode u Zemunu zbog popravke",
        "Когда: 18 ноября до 15:00. Автоцистерна приедет на улицу Пробную.",
        None,
    ),
])
def test_water_notice_scope_is_checked_after_independent_approval(
    source_title: str, body: str, reason: str | None,
) -> None:
    payload = package_payload()
    url = (
        "https://www.beograd.rs/lat/servisne-informacije/vodovod-i-kanalizacija/"
        "a116702/test.html"
    )
    payload["evidence"].update(title=source_title, url=url)
    for claim in payload["outcome"]["claims"]["claims"]:
        for citation in claim["citations"]:
            citation["url"] = url
    payload["outcome"]["draft"].update(title="Отключение воды в Земуне", body=body)
    bind_facts_check(payload)
    digest = draft_sha256(parse(payload).outcome.draft)
    for review in payload["reviews"]:
        review["draft_sha256"] = digest
    bind_context(payload)

    result = evaluate_package(parse(payload), now=NOW)
    if reason is None:
        assert result.status == "ready"
    else:
        assert result.status == "human_review"
        assert result.reason == reason


def test_v1_serialization_preserves_historical_hash_contract() -> None:
    import hashlib

    from relocant_media.ai.package import canonical_json

    package = parse(package_payload())
    payload = package.model_dump(mode="json", exclude={"reviews"})
    assert "event" not in payload
    assert context_sha256(package) == hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def event_payload() -> dict:
    payload = package_payload()
    payload.update(version=2, policy_version="subscription-editorial-v2")
    payload["event"] = {
        "event_key": "a" * 64, "editorial_rule_version": "belgrade-editorial-v2",
        "documents": [{**payload["evidence"], "raw_item_id": 1,
                       "ownership": "official", "high_risk": False, "auto_publish": True}],
    }
    bind_context(payload)
    return payload


@pytest.mark.parametrize("field", ["body", "high_risk", "event_key"])
def test_v2_reviews_bind_the_entire_bundle(field: str) -> None:
    payload = event_payload()
    # Add a separately reviewed source; a later change invalidates both reviews.
    other = {**payload["event"]["documents"][0], "raw_item_id": 2,
             "source_id": "other", "url": "https://other.example/event"}
    payload["event"]["documents"].append(other)
    bind_context(payload)
    if field == "event_key":
        payload["event"][field] = "b" * 64
    else:
        other[field] = "changed" if field == "body" else True
    with pytest.raises(ValueError, match="review context"):
        evaluate_package(parse(payload), now=NOW)


def test_v2_citations_can_use_reviewed_secondary_source() -> None:
    payload = event_payload()
    other = {**payload["event"]["documents"][0], "raw_item_id": 2,
             "source_id": "other", "url": "https://other.example/event"}
    payload["event"]["documents"].append(other)
    citation = payload["outcome"]["claims"]["claims"][0]["citations"][0]
    citation.update(source_id=other["source_id"], url=other["url"])
    bind_facts_check(payload)
    bind_context(payload)
    assert evaluate_package(parse(payload), now=NOW).status == "ready"


@pytest.mark.parametrize("mutation", ["missing_primary", "duplicate", "wrong_policy", "v1_bundle"])
def test_v2_rejects_ambiguous_bundle(mutation: str) -> None:
    payload = event_payload()
    if mutation == "missing_primary":
        payload["event"]["documents"][0]["raw_item_id"] = 2
    elif mutation == "duplicate":
        payload["event"]["documents"].append(payload["event"]["documents"][0].copy())
    elif mutation == "wrong_policy":
        payload["policy_version"] = "subscription-editorial-v1"
    else:
        payload.update(version=1, policy_version="subscription-editorial-v1")
    with pytest.raises(ValidationError):
        parse(payload)


@pytest.mark.parametrize("mutation", [
    "stale", "urgent_stale", "future", "live_unchecked", "live_missing", "flag", "score",
    "hold", "high_risk", "human_status", "not_selected", "other_category", "low_confidence",
])
def test_package_cannot_assert_ready(mutation: str) -> None:
    payload = package_payload()
    review = payload["reviews"][0]
    if mutation == "stale":
        review["reviewed_at"] = (NOW - timedelta(hours=25)).isoformat()
    elif mutation == "urgent_stale":
        payload["urgent"] = True
        review["reviewed_at"] = (NOW - timedelta(hours=3)).isoformat()
    elif mutation == "future":
        review["reviewed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif mutation == "live_unchecked":
        review["live_source_checked"] = False
    elif mutation == "live_missing":
        del review["live_source_checked"]
    elif mutation == "flag":
        review["flags"] = ["uncertain_date"]
    elif mutation == "score":
        review["score"] = 74
    elif mutation == "hold":
        review["verdict"] = "hold"
    elif mutation == "high_risk":
        payload["outcome"]["ranking"]["category"] = "immigration"
    elif mutation == "human_status":
        payload["outcome"]["status"] = "human_review"
    elif mutation == "not_selected":
        payload["outcome"]["ranking"]["should_process"] = False
    elif mutation == "low_confidence":
        payload["outcome"]["verification"]["claims"][0]["confidence"] = 0.7
    else:
        payload["outcome"]["ranking"]["category"] = "utilities"
    bind_context(payload)
    assert evaluate_package(parse(payload), now=NOW).status == "human_review"


@pytest.mark.parametrize("field", ["evidence", "category", "urgent", "verification"])
def test_reviews_cannot_be_reused_after_context_change(field: str) -> None:
    payload = package_payload()
    if field == "evidence":
        payload["evidence"]["body"] += " Additional source context."
    elif field == "category":
        payload["outcome"]["ranking"]["category"] = "events"
    elif field == "verification":
        payload["outcome"]["verification"]["claims"][0]["confidence"] = 0.98
    else:
        payload["urgent"] = True
    with pytest.raises(ValueError, match="review context"):
        evaluate_package(parse(payload), now=NOW)


@pytest.mark.parametrize("mutation", [
    "same_identity", "same_role", "draft_changed", "article", "quote", "claim_ids", "unsupported",
])
def test_invalid_package_contract_is_rejected(mutation: str) -> None:
    payload = package_payload()
    if mutation == "same_identity":
        payload["reviews"][1]["reviewer_id"] = " AGENT-FACTS "
    elif mutation == "same_role":
        payload["reviews"][1]["role"] = "facts"
    elif mutation == "draft_changed":
        payload["outcome"]["draft"]["body"] += "!"
    elif mutation == "article":
        payload["reviews"][0]["article_id"] = "other"
    elif mutation == "quote":
        payload["outcome"]["claims"]["claims"][0]["citations"][0]["quote"] = "invented"
    elif mutation == "claim_ids":
        payload["outcome"]["verification"]["claims"][0]["claim_id"] = "unknown"
    else:
        payload["outcome"]["verification"]["claims"][0]["supported"] = False
    with pytest.raises(ValueError):
        evaluate_package(parse(payload), now=NOW)


@pytest.mark.parametrize("mutation", [
    "extra", "nested_extra", "coercion", "naive", "version", "version_bool",
])
def test_strict_json_boundary(mutation: str) -> None:
    payload = package_payload()
    if mutation == "extra":
        payload["unknown"] = True
    elif mutation == "nested_extra":
        payload["outcome"]["claims"]["claims"][0]["extra"] = True
    elif mutation == "coercion":
        payload["reviews"][0]["score"] = "80"
    elif mutation == "naive":
        payload["reviews"][0]["reviewed_at"] = "2026-10-19T08:00:00"
    elif mutation == "version_bool":
        payload["version"] = True
    else:
        payload["version"] = 2
    with pytest.raises(ValidationError):
        parse(payload)


def test_package_cli_preview_does_not_load_database(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    import builtins
    import sys

    from relocant_media.ops.package_cli import main

    package_path = tmp_path / "package.json"
    package_path.write_text(json.dumps(package_payload()), encoding="utf-8")
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name in {"relocant_media.storage.db", "relocant_media.ai.gemini"}:
            raise AssertionError("preview must not initialize DB or Gemini")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(sys, "argv", ["relocant-package", "--package", str(package_path)])
    main()
    assert json.loads(capsys.readouterr().out)["mode"] == "preview"


def test_package_cli_fake_requires_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from relocant_media.ops.package_cli import main

    monkeypatch.setattr(sys, "argv", [
        "relocant-package", "--package", "does-not-exist.json", "--transport", "fake",
    ])
    with pytest.raises(SystemExit, match="2"):
        main()


def _rebind_draft(payload: dict) -> None:
    from relocant_media.ai.models import RussianDraft

    draft = RussianDraft.model_validate(payload["outcome"]["draft"])
    for review in payload["reviews"]:
        review.update(draft_sha256=draft_sha256(draft), score=100)
    bind_facts_check(payload)
    bind_context(payload)


def test_city_category_cannot_auto_publish_missing_minor_story() -> None:
    # Обезличенный пример из #319: модель выбрала city, пост ушёл автопубликацией.
    payload = package_payload()
    payload["outcome"]["ranking"]["category"] = "city"
    payload["outcome"]["draft"]["body"] = (
        "Полиция ищет 17-летнего подростка из Земуна, страдающего аутизмом. "
        + payload["outcome"]["draft"]["body"])
    _rebind_draft(payload)
    result = evaluate_package(parse(payload), now=NOW)
    assert result.status == "human_review"
    assert result.reason == "sensitive_topic_requires_human_review"


def test_serbian_source_signal_holds_even_with_neutral_draft() -> None:
    payload = package_payload()
    payload["evidence"]["title"] = "Nestao dečak (14) iz Zemuna, Pronađi me traži pomoć"
    bind_facts_check(payload)
    bind_context(payload)
    result = evaluate_package(parse(payload), now=NOW)
    assert result.status == "human_review"
    assert result.reason == "sensitive_topic_requires_human_review"


def test_approved_package_cannot_deliver_implicit_health_details_of_a_minor() -> None:
    payload = package_payload()
    payload["outcome"]["ranking"]["category"] = "city"
    payload["outcome"]["draft"]["title"] = "15-летний пассажир пострадал"
    payload["outcome"]["draft"]["body"] = "Ему назначили лекарства."
    _rebind_draft(payload)
    result = evaluate_package(parse(payload), now=NOW)
    assert result.status == "human_review"
    assert result.reason == "sensitive_personal_detail_in_draft"
