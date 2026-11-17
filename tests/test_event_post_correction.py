import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_correction_delivery import (
    TOKEN,
)
from test_correction_delivery import (
    frozen as correction_frozen,
)
from test_correction_delivery import ready_package as correction_ready_package
from test_correction_delivery import seeded as correction_seeded

from relocant_media.ai.package import canonical_json, context_sha256, draft_sha256
from relocant_media.bot.formatting import editorial_caption
from relocant_media.domain.map_links import map_event_id
from relocant_media.ops import correction_delivery as worker
from relocant_media.ops.event_post_correction import (
    EventPostCorrectionPlan,
    prepared_event_edit,
    reviewed_event,
)
from relocant_media.ops.events_export import EventsFeed, ReviewedMapEvent
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.storage.models import EditorialRun, Post, RawItem

frozen = correction_frozen
ready_package = correction_ready_package
seeded = correction_seeded
NOW = datetime(2026, 11, 18, 10, tzinfo=UTC)


@pytest.fixture
def event_edit(frozen, tmp_path, monkeypatch):
    engine, storage, _, _, _ = frozen
    with Session(engine) as session:
        run = session.scalar(select(EditorialRun))
        post = session.get(Post, run.post_id)
        raw = session.get(RawItem, run.raw_item_id)
        payload = copy.deepcopy(run.outcome["_package"])
        payload["outcome"]["ranking"]["category"] = "events"
        package = worker.EditorialPackage.model_validate_json(canonical_json(payload))
        package = package.model_copy(update={"reviews": tuple(
            review.model_copy(update={"context_sha256": context_sha256(package),
                                      "draft_sha256": draft_sha256(package.outcome.draft)})
            for review in package.reviews)})
        package_sha = worker._digest(package.model_dump(mode="json"))
        run.outcome = {**run.outcome, "_package": package.model_dump(mode="json"),
                       "_package_sha256": package_sha}
        post.category = "events"
        session.commit()
        old_text, _ = editorial_caption(
            f"{post.title}\n\n{post.body}\n\nИсточник: {raw.url}")
        record = {"message_id": post.telegram_message_id, "post_title": post.title,
            "title": "Концерт · дату нужно уточнить", "source_url": raw.url,
            "starts_at": "2026-11-25T20:00:00+01:00", "ends_at": None,
            "venue": {"name": "Клуб", "address": "Белград, 30", "latitude": 44.82,
                      "longitude": 20.45},
            "coordinate_source_url": "https://www.openstreetmap.org/node/123",
            "venue_source_url": "https://example.org/venue",
            "schedule_source_url": "https://example.org/tickets",
            "schedule_note": "В СМИ 24 ноября, у организатора 25 ноября. Уточните дату.",
            "evidence_urls": ["https://example.org/organizer", "https://example.org/tickets"]}
        plan = {"version": 3, "city": "belgrade", "raw_item_id": raw.id,
            "message_id": post.telegram_message_id, "old_package_sha256": package_sha,
            "old_text_sha256": hashlib.sha256(old_text.encode()).hexdigest(),
            "title": post.title, "body": "Когда: у организатора 25 ноября. В СМИ 24 ноября.",
            "source_url": raw.url, "map_event_sha256": worker._digest(record),
            "evidence": [{"url": url, "text": "25 ноября в 20:00 — Клуб"}
                         for url in record["evidence_urls"]]}
        context = worker._digest(plan)
        plan["reviews"] = [{"reviewer_id": role, "role": role, "context_sha256": context,
            "reviewed_at": NOW.isoformat(), "verdict": "approve", "score": 95,
            "flags": [], "live_source_checked": True} for role in ("facts", "utility")]
    data = canonical_json(plan).encode()
    sha = hashlib.sha256(data).hexdigest()
    database = Path(engine.url.database)
    snapshot = tmp_path / "event-snapshot"
    manifest = create_snapshot(database, snapshot)
    storage.state["snapshot"] = {**manifest, "drive_file_id": manifest["sha256"]}
    storage.blobs[manifest["sha256"]] = (snapshot / "editorial.sqlite3").read_bytes()
    storage.blobs[sha] = data
    registry = tmp_path / "cities/belgrade/reviewed-map-events.json"
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps([record]), encoding="utf8")
    monkeypatch.chdir(tmp_path)
    return engine, storage, plan, sha, record, old_text, database


def embed(post_id, visible, source):
    import html

    text = html.escape(visible.removesuffix("Источник")).replace("\n", "<br>")
    return (f'<div data-post="eduvbelgrad/{post_id}" class="tgme_widget_message">'
            f'<div class="tgme_widget_message_text">{text}'
            f'<a href="{source}">Источник</a></div></div>')


@pytest.mark.parametrize("mutation", [None, "feed", "source", "public", "state", "review"])
def test_event_correction_is_audited_and_fail_closed(event_edit, tmp_path, mutation):
    _, storage, plan, sha, record, old, database = event_edit
    before = database.read_bytes()
    if mutation == "state":
        storage.change_state = True
    if mutation == "review":
        plan["reviews"][0]["context_sha256"] = "0" * 64
        content = canonical_json(plan).encode()
        sha = hashlib.sha256(content).hexdigest()
        storage.blobs[sha] = content
    card = ReviewedMapEvent.model_validate(record).card("belgrade", "Europe/Belgrade", NOW)
    feed = EventsFeed(generated_at=NOW, events=() if mutation == "feed" else (card,))
    sends = []

    def respond(request):
        if request.method == "GET":
            if str(request.url).endswith("events.json"):
                return httpx.Response(200, text=feed.model_dump_json())
            if request.url.host == "example.org":
                return httpx.Response(200, text="Изменено" if mutation == "source"
                                      else "<p>25 ноября в 20:00 — Клуб</p>")
            return httpx.Response(200, text=embed(plan["message_id"],
                "Иной текст\n\nИсточник" if mutation == "public" else old, plan["source_url"]))
        sends.append(json.loads(request.content))
        payload = sends[-1]
        assert request.url.path.endswith("/editMessageText")
        return httpx.Response(200, json={"ok": True, "result": {
            "message_id": payload["message_id"],
            "chat": {"id": payload["chat_id"], "type": "channel"},
            "text": payload["text"], "entities": list(reversed(payload["entities"])),
            "reply_markup": payload["reply_markup"]}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        if mutation:
            with pytest.raises(ValueError):
                worker.execute(storage, sha, tmp_path / "edit", client, token=TOKEN, now=NOW)
            assert sends == []
        else:
            result = worker.execute(storage, sha, tmp_path / "edit", client, token=TOKEN, now=NOW)
            assert result["status"] == "corrected"
            assert len(sends) == 1 and sends[0]["message_id"] == plan["message_id"]
            assert sends[0]["reply_markup"]["inline_keyboard"][0][0]["url"].endswith(
                "places_" + map_event_id(plan["source_url"]))
            assert [audit.get("operation") for audit in storage.audits] == ["editMessageText", None]
    assert database.read_bytes() == before


def test_registry_and_original_package_are_bound_to_correction(event_edit):
    engine, _, value, _, record, _, _ = event_edit
    plan = EventPostCorrectionPlan.model_validate_json(canonical_json(value))
    event = reviewed_event(plan)
    assert event.card("belgrade", "Europe/Belgrade", NOW).expires_at.isoformat() == (
        "2026-11-26T00:00:00+01:00")
    with Session(engine) as session:
        edit = prepared_event_edit(session, plan, event)
        assert "Уточните" not in edit["payload"]["text"]
        for field in ("old_text_sha256", "old_package_sha256"):
            changed = plan.model_copy(update={field: "0" * 64})
            with pytest.raises(ValueError):
                prepared_event_edit(session, changed, event)
    with pytest.raises(ValueError, match="реестром"):
        reviewed_event(plan.model_copy(update={"evidence": (
            plan.evidence[0].model_copy(update={"url": "https://evil.example/new"}),
            plan.evidence[1],
        )}))
    registry = Path("cities/belgrade/reviewed-map-events.json")
    registry.write_text(json.dumps([{**record, "title": "Подмена"}]), encoding="utf8")
    with pytest.raises(ValueError, match="записью карты"):
        reviewed_event(plan)
