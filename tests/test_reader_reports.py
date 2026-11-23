from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_reader_publication import material

from relocant_media.domain.policy import (
    OWNER_DECISION_REASONS,
    READER_REPORT_REVIEW_REASON,
    primary_evidence_kind,
)
from relocant_media.services.editorial import _auto_publish_policy_reason
from relocant_media.services.owner_review_prepare import assert_primary_evidence
from relocant_media.services.reader_publication import material_fingerprint
from relocant_media.services.reader_reports import (
    HEADER,
    ensure_reader_source,
    ineligible_reason,
    materialize_reader_reports,
    reader_report_delivery_reason,
    reader_source_id,
    report_body,
    submission_id_for,
    with_reader_credit,
)
from relocant_media.storage.models import RawItem, Source, Submission
from relocant_media.storage.sqlite_schema import initialize_sqlite

NOW = datetime(2026, 11, 23, 18, tzinfo=UTC)
TEXT = ("На Дорчоле на улице Цара Душана открылась маленькая русская библиотека, "
        "книги можно брать бесплатно по субботам.")


def reader_payload(*, text=TEXT, mode="anonymous", publication=None, **changes):
    payload = {**material(mode=mode), "text": text, "attachments": [],
               "accepted_at": (NOW - timedelta(hours=2)).isoformat(), **changes}
    payload["reader_publication"].update(publication or {})
    payload["reader_publication"]["material_sha256"] = material_fingerprint(payload)
    return payload


@pytest.fixture
def session(tmp_path):
    path = tmp_path / "editorial.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        yield session
    engine.dispose()


def add(session, payload, status="reader_review"):
    row = Submission(kind="news_tip", status=status, payload=payload)
    session.add(row)
    session.flush()
    return row


def test_permitted_reader_text_becomes_an_attributed_document_once(session):
    row = add(session, reader_payload(mode="username"))
    created = materialize_reader_reports(session, city="belgrade", now=NOW)
    session.commit()
    assert len(created) == 1
    raw = session.get(RawItem, created[0])
    source = session.get(Source, raw.source_id)
    assert (source.id, source.ownership, source.source_type) == (
        reader_source_id("belgrade"), "reader", "reader_report")
    assert source.auto_publish and not source.high_risk
    assert raw.body.startswith(HEADER) and TEXT in raw.body
    assert "Подпись, выбранная автором для публикации: Материал: @reader_alias" in raw.body
    # No private identity leaves the submission.
    assert "42" not in raw.url and str(row.payload["chat_id"]) not in raw.body
    assert submission_id_for(raw) == row.id
    assert row.status == "source_linked" and row.payload["raw_item_ids"] == [raw.id]
    assert row.payload["reader_report"]["material_sha256"] == material_fingerprint(row.payload)
    assert materialize_reader_reports(session, city="belgrade", now=NOW) == []
    assert len(session.scalars(select(RawItem)).all()) == 1


@pytest.mark.parametrize(("payload", "reason"), [
    (reader_payload(publication={"permission": "private"}), "no_publication_permission"),
    (reader_payload(publication={"ownership": "source"}), "no_publication_permission"),
    (reader_payload(text="Коротко"), "text_too_short"),
    (reader_payload(text=TEXT + " https://example.com/very/long/link/that/is/not/text"
                    ), None),
    (reader_payload(text=TEXT + " Звоните +381 64 123 4567."), "contact_details_present"),
    (reader_payload(text=TEXT + " Пишите anna@example.com"), "contact_details_present"),
    (reader_payload(accepted_at=(NOW - timedelta(days=8)).isoformat()), "submission_too_old"),
    (reader_payload(test_only=True), "not_reader_material"),
])
def test_only_permitted_contact_free_recent_text_is_eligible(session, payload, reason):
    row = add(session, payload)
    assert ineligible_reason(row, now=NOW) == reason
    created = materialize_reader_reports(session, city="belgrade", now=NOW)
    assert bool(created) is (reason is None)
    assert row.status == ("source_linked" if reason is None else "reader_review")


def test_delivery_binds_draft_to_unchanged_reader_material(session):
    row = add(session, reader_payload())
    raw = session.get(RawItem, materialize_reader_reports(session, city="belgrade", now=NOW)[0])
    body = "Как рассказал наш читатель, на Дорчоле открылась библиотека.\n\n#Город"
    assert reader_report_delivery_reason(row, raw, title="Библиотека", body=body) is None
    assert "attributed" in reader_report_delivery_reason(
        row, raw, title="Библиотека", body="На Дорчоле открылась библиотека.")
    assert "contact" in reader_report_delivery_reason(
        row, raw, title="Библиотека", body=body + " Тел. +381 64 123 4567")
    changed = {**row.payload, "text": TEXT + " Дополнение."}
    row.payload = changed
    assert "no longer matches" in reader_report_delivery_reason(
        row, raw, title="Библиотека", body=body)
    assert raw.body != report_body(changed)


def test_reader_evidence_publishes_alone_only_in_low_risk_categories(session):
    reader = ensure_reader_source(session, "belgrade")
    official = Source(id="official", city="belgrade", name="o", url="https://example.rs",
                      source_type="official_news", trust=Decimal("0.9"),
                      ownership="official", high_risk=False, auto_publish=True)
    members = [(None, reader)]
    for category in ("city", "culture", "events", "sport"):
        assert primary_evidence_kind(["reader"], category) == "reader"
        assert _auto_publish_policy_reason(members, anchor_source=reader, category=category) is None
    for category in ("transport", "utilities", "medicine", "politics"):
        assert primary_evidence_kind(["reader"], category) is None
        assert _auto_publish_policy_reason(
            members, anchor_source=reader, category=category) == READER_REPORT_REVIEW_REASON
    assert READER_REPORT_REVIEW_REASON in OWNER_DECISION_REASONS
    # A public official document still carries the event as usual.
    assert _auto_publish_policy_reason(
        [(None, reader), (None, official)], anchor_source=reader, category="transport") is None


def test_owner_can_release_an_attributed_reader_report_outside_low_risk():
    draft = type("Draft", (), {"title": "Т", "body": "Читатель сообщает"})()
    assert assert_primary_evidence(["reader"], "transport", draft, []) == "reader"
    with pytest.raises(Exception, match="official"):
        assert_primary_evidence(["reader"], "medicine", draft, [])


def test_credit_line_is_placed_before_topic_footer():
    body = "Как рассказал наш читатель, открылась библиотека.\n\n#Город"
    assert with_reader_credit(body, "Материал: Анна") == (
        "Как рассказал наш читатель, открылась библиотека.\n\nМатериал: Анна\n\n#Город")
    assert with_reader_credit(body, "") == body
    assert with_reader_credit("Текст", "Материал: Анна") == "Текст\n\nМатериал: Анна"
