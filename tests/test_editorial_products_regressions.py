from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_server_cycle import seeded as server_seeded

from relocant_media.ai.package import context_sha256, draft_sha256
from relocant_media.domain.dedup import find_repeated_publication
from relocant_media.ops import server_cycle
from relocant_media.services.editorial_products import present_draft, recent_publications
from relocant_media.storage.models import EditorialRun, Post, RawItem

seeded = server_seeded


def _history_row(
    session, source_id, token, *, created_at, published_at=None,
    status="ready", delivery_status="sent", body="Опубликованная городская новость.",
):
    raw = RawItem(
        source_id=source_id, external_id=f"history-{token}",
        url=f"https://example.com/history/{token}",
        title=f"Отдельный источник {token}", body=f"Самостоятельный исходник {token}",
        published_at=None,
    )
    post = Post(
        city="belgrade", category="transport", status=status,
        title=f"Публикация {token}", body=body, confidence=Decimal("0.99"),
        created_at=created_at, published_at=published_at,
    )
    session.add_all([raw, post])
    session.flush()
    session.add(EditorialRun(
        raw_item_id=raw.id, post_id=post.id, evidence={},
        outcome={"status": status, "ranking": {"selection": {"rubric_id": "city_changes"}}},
        delivery_status=delivery_status,
    ))
    return post


def test_forty_newer_held_drafts_do_not_hide_published_history(seeded):
    engine, raw_id, _ = seeded
    now = datetime.now(UTC)
    with Session(engine) as session:
        source_id = session.get(RawItem, raw_id).source_id
        sent = _history_row(
            session, source_id, "sent", created_at=now - timedelta(hours=2),
            published_at=now - timedelta(hours=1),
        )
        queued = _history_row(
            session, source_id, "queued", created_at=now - timedelta(minutes=50),
            delivery_status="pending",
        )
        for index in range(40):
            _history_row(
                session, source_id, f"held-{index}",
                created_at=now - timedelta(minutes=40 - index),
                status="human_review", delivery_status="pending",
            )
        session.commit()

        history = recent_publications(session, "belgrade", now=now)

        assert [(item["id"], item["status"]) for item in history] == [
            (queued.id, "ready"), (sent.id, "published"),
        ]
        assert all(item["rubric_id"] == "city_changes" for item in history)


def test_history_window_uses_actual_publication_date_for_delayed_draft(seeded):
    engine, raw_id, _ = seeded
    now = datetime.now(UTC)
    with Session(engine) as session:
        source_id = session.get(RawItem, raw_id).source_id
        delayed = _history_row(
            session, source_id, "delayed", created_at=now - timedelta(days=10),
            published_at=now - timedelta(hours=1),
        )
        _history_row(
            session, source_id, "old", created_at=now - timedelta(days=10),
            published_at=now - timedelta(days=8),
        )
        session.commit()

        history = recent_publications(session, "belgrade", now=now)

        assert [item["id"] for item in history] == [delayed.id]
        assert datetime.fromisoformat(history[0]["published_at"]) == now - timedelta(hours=1)


def test_duplicate_created_by_repair_is_rejected_before_package_import(
    seeded, tmp_path, monkeypatch,
):
    engine, raw_id, provider = seeded
    now = datetime.now(UTC)
    previous_body = provider._responses["draft"].body
    provider._responses["draft"] = provider._responses["draft"].model_copy(update={
        "body": previous_body + " После работ компания обещает проверить качество воды.",
    })
    with Session(engine) as session:
        source_id = session.get(RawItem, raw_id).source_id
        previous = _history_row(
            session, source_id, "previous", created_at=now - timedelta(hours=2),
            published_at=now - timedelta(hours=1), body=previous_body,
        )
        session.commit()
        previous_id, previous_title = previous.id, previous.title

    repairs = []

    def repair_and_review(package, provider, live_documents, **kwargs):
        # Первый текст ещё содержит добавочную деталь и не совпадает с прошлым постом.
        assert find_repeated_publication(
            package.outcome.draft.title, package.outcome.draft.body,
            [(previous_id, previous_title, previous_body)],
        ) is None
        repaired = present_draft(
            package.outcome.draft.model_copy(update={"body": previous_body}),
            package.presentation,
            category=package.outcome.ranking.category,
        )
        package = package.model_copy(update={
            "outcome": package.outcome.model_copy(update={"draft": repaired}),
        })
        digest = context_sha256(package)
        reviews = tuple(review.model_copy(update={
            "context_sha256": digest, "draft_sha256": draft_sha256(repaired),
            "reviewed_at": datetime.now(UTC), "verdict": "approve", "score": 94,
            "flags": (), "live_source_checked": True,
        }) for review in package.reviews)
        repairs.append(True)
        return package.model_copy(update={"reviews": reviews})

    def forbidden_import(*args, **kwargs):
        raise AssertionError("Повтор после ремонта не должен попадать в импорт готового пакета")

    monkeypatch.setattr(server_cycle, "review_with_repairs", repair_and_review)
    monkeypatch.setattr(server_cycle, "import_package", forbidden_import)

    result = server_cycle.prepare_reviewed(engine, raw_id, provider, tmp_path)

    assert repairs == [True]
    assert provider.calls == ["rank", "claims", "verify", "draft"]
    assert result["status"] == "skipped"
    assert result["reason"] == f"draft_repeats_recent_post:{previous_id}"
    with Session(engine) as session:
        run = session.get(EditorialRun, raw_id)
        assert run.outcome["status"] == "skipped"
        assert run.outcome["draft"] is None
        assert run.post_id is None
        assert session.scalars(select(Post.id)).all() == [previous_id]
