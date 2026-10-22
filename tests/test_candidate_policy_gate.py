from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session

from relocant_media.ops.candidates_cli import select_candidates
from relocant_media.storage.models import RawItem, Source

editorial_session = _editorial_session
NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def test_manual_review_sees_shadow_but_autonomous_queue_does_not(
    editorial_session: Session,
) -> None:
    session = editorial_session
    session.add_all(
        [
            Source(
                id="auto-source",
                city="belgrade",
                name="Auto source",
                url="https://auto.example.org",
                source_type="official",
                trust=1,
                enabled=True,
                auto_publish=True,
                high_risk=False,
            ),
            Source(
                id="shadow-source",
                city="belgrade",
                name="Shadow source",
                url="https://shadow.example.org",
                source_type="official",
                trust=1,
                enabled=True,
                auto_publish=False,
                high_risk=True,
            ),
        ]
    )
    session.flush()
    session.add_all(
        [
            RawItem(
                source_id="auto-source",
                external_id="auto-item",
                url="https://auto.example.org/item",
                title="Independent automatic item",
                body="Verified automatic source evidence",
                published_at=NOW,
            ),
            RawItem(
                source_id="shadow-source",
                external_id="shadow-item",
                url="https://shadow.example.org/item",
                title="Independent shadow item",
                body="Verified high-risk shadow evidence",
                published_at=NOW + timedelta(minutes=1),
            ),
        ]
    )
    session.commit()

    manual = select_candidates(
        session,
        city="belgrade",
        since=NOW - timedelta(hours=1),
        until=NOW + timedelta(hours=1),
        limit=10,
    )
    autonomous = select_candidates(
        session,
        city="belgrade",
        since=NOW - timedelta(hours=1),
        until=NOW + timedelta(hours=1),
        limit=10,
        auto_publish_only=True,
    )

    assert {item["article_id"] for item in manual["candidates"]} == {
        "auto-item",
        "shadow-item",
    }
    assert [item["article_id"] for item in autonomous["candidates"]] == ["auto-item"]
    assert autonomous["missing_published_at_scope"].startswith("auto-publish")
