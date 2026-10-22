import hashlib
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.media_review_packet import (
    build_media_review_packet,
    pending_media_review_ids,
    plan_reviewed_packet,
)
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem, Source

SOURCE_PAGE = "https://official.example/article/42"
IMAGE_URL = "https://official.example/media/event.jpg"


def _ready_record(session: Session, *, raw_external_id: str = "42") -> int:
    source = session.get(Source, "official")
    if source is None:
        session.add(Source(
            id="official",
            city="belgrade",
            name="Official",
            url="https://official.example/",
            source_type="official_city",
            trust=Decimal("1.000"),
            ownership="government",
            high_risk=False,
            auto_publish=False,
            enabled=True,
        ))
        session.flush()
    raw = RawItem(
        source_id="official",
        external_id=raw_external_id,
        url=SOURCE_PAGE,
        canonical_url=SOURCE_PAGE,
        content_sha256="b" * 64,
        title="Official update",
        body="Verified body",
        published_at=None,
    )
    session.add(raw)
    session.flush()
    post = Post(
        city="belgrade",
        category="city",
        status="ready",
        title="Title",
        body="Body",
        confidence=Decimal("0.9000"),
    )
    session.add(post)
    session.flush()
    session.add(EditorialRun(
        raw_item_id=raw.id,
        outcome={"status": "ready", "_package_sha256": "a" * 64},
        evidence={"documents": [{"raw_item_id": raw.id}]},
        post_id=post.id,
        delivery_status="pending",
    ))
    html = "<article><img src='/media/event.jpg'></article>"
    encoded = html.encode()
    session.add(RawItemMediaSnapshot(
        raw_item_id=raw.id,
        source_page=SOURCE_PAGE,
        source_html=html,
        source_html_sha256=hashlib.sha256(encoded).hexdigest(),
        source_html_bytes=len(encoded),
        manifest={
            "version": 1,
            "source_page": SOURCE_PAGE,
            "source_html_sha256": hashlib.sha256(encoded).hexdigest(),
            "source_html_bytes": len(encoded),
            "candidate_extraction_network_access_performed": False,
            "generated_media_used": False,
            "publication_permitted": False,
            "candidates": [{
                "url": IMAGE_URL,
                "source_page": SOURCE_PAGE,
                "origin": "article_image",
                "alt": "Event",
                "attribution": "Foto: Official",
                "provenance_basis": "article_img",
                "rights_hint": "explicit_attribution_requires_rights_review",
                "event_match": "unreviewed",
                "declared_width": 1200,
                "declared_height": 800,
            }],
        },
    ))
    session.commit()
    return raw.id


def test_ready_run_materializes_fail_closed_review_template() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        raw_id = _ready_record(session)
        packet = build_media_review_packet(session, raw_id)

    assert packet["mode"] == "needs_visual_review"
    assert packet["publication_permitted"] is False
    assert packet["download_permitted"] is False
    assert packet["editorial_package_sha256"] == "a" * 64
    assert len(packet["candidates"]) == 1
    template = packet["candidates"][0]["assessment_template"]
    assert template["kind"] == "unknown"
    assert template["event_match"] is False
    assert template["rights_status"] == "needs_review"
    assert template["rights_method"] == "unknown"
    assert template["assessed_by"] == ""
    assert "rights_verified" not in str(packet)
    with pytest.raises(ValidationError):
        CoverAssessment.model_validate(template)
    engine.dispose()


def test_packet_rejects_assessment_not_bound_to_saved_candidate() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        raw_id = _ready_record(session)
        packet = build_media_review_packet(session, raw_id)
    assessment = CoverAssessment(
        image_url="https://evil.example/not-in-snapshot.jpg",
        source_page=SOURCE_PAGE,
        kind="current_photo",
        event_match=True,
        event_year=2026,
        visual_value=4,
        rights_method="permission",
        rights_status="reviewed",
        rights_evidence_url="https://official.example/rights/42",
        rights_rationale="Explicit permission recorded for this exact image.",
        attribution="Official",
        assessed_by="reviewer-1",
    )
    with pytest.raises(ValueError, match="not bound"):
        plan_reviewed_packet(packet, (assessment,), event_year=2026)
    engine.dispose()


def test_explicit_bound_assessment_can_enter_existing_cover_plan() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        raw_id = _ready_record(session)
        packet = build_media_review_packet(session, raw_id)
    assessment = CoverAssessment(
        image_url=IMAGE_URL,
        source_page=SOURCE_PAGE,
        kind="current_photo",
        event_match=True,
        event_year=2026,
        visual_value=4,
        rights_method="permission",
        rights_status="reviewed",
        rights_evidence_url="https://official.example/rights/42",
        rights_rationale="Explicit permission recorded for this exact image.",
        attribution="Official",
        assessed_by="reviewer-1",
    )
    plan = plan_reviewed_packet(packet, (assessment,), event_year=2026)
    assert plan["mode"] == "single_cover"
    assert plan["selected"][0]["image_url"] == IMAGE_URL
    assert plan["requires_independent_media_reviews"] is True
    engine.dispose()


def test_tampered_snapshot_fails_closed_to_text() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        raw_id = _ready_record(session)
        snapshot = session.get(RawItemMediaSnapshot, raw_id)
        assert snapshot is not None
        snapshot.source_html = "tampered"
        session.commit()
        packet = build_media_review_packet(session, raw_id)

    assert packet["mode"] == "text"
    assert packet["candidates"] == []
    assert packet["requires_independent_media_reviews"] is False
    engine.dispose()


def test_pending_queue_is_derived_from_ready_runs_with_valid_candidates() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        raw_id = _ready_record(session)
        assert pending_media_review_ids(session) == (raw_id,)
        run = session.get(EditorialRun, raw_id)
        assert run is not None
        run.outcome = {**run.outcome, "status": "human_review"}
        session.commit()
        assert pending_media_review_ids(session) == ()
    engine.dispose()
