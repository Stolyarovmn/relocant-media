import pytest
from test_channel_delivery import FakePublisher, send
from test_channel_delivery import reviewed as _reviewed

from relocant_media.ops.cycle_audit import audit_cycle
from relocant_media.storage.models import Post

reviewed = _reviewed


def report(ids):
    return dict(
        status="PRIMARY_PARTIAL",
        processing_errors=[],
        fallback_required=False,
        sent_message_ids=ids,
        pin_maintenance=dict(blocked=[], owner_action_required=False),
    )


@pytest.mark.asyncio
async def test_audit_checks_real_sent_receipt_and_russian_text(reviewed):
    session, run = reviewed
    await send(session, run, FakePublisher(result=42))
    result = audit_cycle(session, report([42]))
    assert result["status"] == "passed" and result["messages"][0]["raw_item_id"] == run.raw_item_id
    assert audit_cycle(session, report([42, 42]))["checks"]["unique_receipts"] is False
    assert audit_cycle(session, report([99]))["checks"]["receipts_match_database"] is False
    post = session.get(Post, run.post_id)
    post.body = "Крајњи рок за завршетак изградње моста је пролеће 2027. године."
    session.flush()
    assert audit_cycle(session, report([42]))["checks"]["russian_copy"] is False


def test_no_posts_is_not_a_verified_new_publication(reviewed):
    session, _ = reviewed
    result = audit_cycle(session, report([]))
    assert result["status"] == "passed_no_publications" and result["messages"] == []
    failed = report([])
    failed["processing_errors"] = [{"raw_item_id": 1}]
    assert audit_cycle(session, failed)["status"] == "requires_review"
