import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from facts_check_helpers import bind_facts_check
from test_editorial_package import bind_context, parse
from test_gpt_fallback_service import fallback_session as _fallback_session
from test_gpt_fallback_service import package_for_request

from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.package import canonical_json, draft_sha256
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.ops import gpt_fallback_worker, server_cycle
from relocant_media.services import gpt_fallback
from relocant_media.services.editorial_products import present_draft
from relocant_media.services.material_lifecycle import lifecycle_state, sweep_materials
from relocant_media.services.native_map import bind_native_map_draft
from relocant_media.services.package_import import import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

fallback_session = _fallback_session


def _candidate(session, raw_id, now):
    return gpt_fallback._candidate_request(
        session, raw_id, now=now, profile=load_city_profile("belgrade"),
        registry=gpt_fallback._registry(),
    )


def _package(candidate, title, body, now, parent=None):
    request = {"candidates": [candidate], "created_at": now.isoformat()}
    base = package_for_request(request)
    payload = base.model_dump(mode="json")
    outcome = payload["outcome"]
    outcome["ranking"]["category"] = "transport"
    outcome["ranking"]["selection"]["novelty"] = "Официально подтверждена отмена рейса"
    outcome["draft"].update(title=title, body=body)
    assert base.presentation is not None
    draft = present_draft(
        base.outcome.draft.model_copy(update={"title": title, "body": body}),
        base.presentation, category="transport",
    )
    outcome["draft"] = draft.model_dump(mode="json")
    outcome["claims"]["claims"][0].update(
        text=candidate["documents"][0]["body"],
        citations=[{"source_id": candidate["source_id"], "url": candidate["documents"][0]["url"],
                    "quote": candidate["documents"][0]["body"]}],
    )
    payload["presentation"] = bind_native_map_draft(
        base.presentation, draft).model_dump(mode="json")
    for review in payload["reviews"]:
        review["draft_sha256"] = draft_sha256(draft)
    bind_facts_check(payload)
    if parent is not None:
        ids = list(draft.claim_ids)
        payload["linked_update"] = {"parent": parent, "change_claim_ids": ids}
        for review in payload["reviews"]:
            if review["role"] == "facts":
                review["facts_check"]["linked_update_check"] = {
                    "parent_message_id": parent["message_id"], "parent_publication_checked": True,
                    "same_event": True, "substantive_new_facts": True, "change_claim_ids": ids,
                    "reason": "В тесте подтверждена отмена обратного рейса.",
                }
    bind_context(payload)
    return parse(payload)


def _answer(request, *, packages=(), rejected=()):
    return gpt_fallback_worker.FallbackInput.model_validate_json(canonical_json({
        "request": request, "result": {
            "version": 1, "request_sha256": hashlib.sha256(
                canonical_json(request).encode()).hexdigest(),
            "packages": [package.model_dump(mode="json") for package in packages],
            "rejected": rejected,
        },
    }))


@pytest.mark.parametrize("resolution, came_from_fallback", [
    ("duplicate", False), ("update", False), ("update", True),
])
def test_held_flight_goes_from_delivery_to_real_bound_fallback(
    fallback_session, monkeypatch, resolution, came_from_fallback,
):
    session, preceding, cycle = fallback_session
    now = datetime.now(UTC)
    flight_day = (now + timedelta(days=2)).strftime("%d.%m.%Y")
    source = session.get(Source, preceding.source_id)
    preceding.title = "Ukinut let za Brisel zbog štrajka"
    preceding.body = "Putnicima će biti ponuđena zamena. Radi se o odlasku iz Beograda."
    preceding.published_at = preceding.ingested_at = now - timedelta(hours=2)
    cycle.finished_at = now - timedelta(minutes=1)
    cycle.scheduled_for = cycle.started_at = now - timedelta(minutes=3)
    cycle.status = "success"
    session.commit()
    parent = import_package(session, _package(_candidate(session, preceding.id, now),
        f"Er Srbija отменила рейс из Белграда в Брюссель на {flight_day}",
        "Пассажирам предложат пересадку. Объявление касается вылета из Белграда.",
        now - timedelta(hours=1)), now=now)
    parent_post = session.get(Post, parent.post_id)
    parent.delivery_status = "sent"
    parent_post.telegram_message_id = 153
    parent_post.published_at = now - timedelta(hours=1)
    assert parent_post.id != 153
    current = RawItem(
        source_id=source.id, external_id="confirmed-return", title="Otkazan i povratni let",
        body="Prevoznik je potvrdio da je ukinut povratni let JU270 iz Brisela.",
        url="https://www.beograd.rs/lat/beoinfo-vesti/a9997/povratni-let.html",
        published_at=now - timedelta(minutes=6), published_at_precision="instant", ingested_at=now,
    )
    session.add(current)
    session.commit()
    raw_id = current.id
    checked = _package(_candidate(session, raw_id, now),
        f"Air Serbia отменила рейсы между Белградом и Брюсселем на {flight_day}",
        "Подтверждена отмена обратного рейса JU270. Пассажиры получат письмо от перевозчика.",
        now)
    def fetch(package):
        return tuple(EvidenceDocument.model_validate(doc.model_dump())
                     for doc in package.event.documents)

    calls = []

    class Bot:
        def __init__(self, *args):
            pass

        async def send_notice_message(self, target, text, *, source_url, reply_to=None):
            calls.append(reply_to)
            return 156

    monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "public")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake-token")
    monkeypatch.setattr(server_cycle, "TelegramBotAPI", Bot)
    monkeypatch.setattr(server_cycle, "morning_release", lambda **kwargs: None)
    monkeypatch.setattr(server_cycle, "pacing_hold", lambda **kwargs: False)
    monkeypatch.setattr(server_cycle, "fetch_documents", fetch)
    monkeypatch.setattr(gpt_fallback_worker.server_runtime, "fetch_documents", fetch)
    engine = session.get_bind()
    if came_from_fallback:
        cycle.status = "partial"
        cycle.details = {"processing_errors": [{"raw_item_id": raw_id,
                                                 "error_type": "GeminiWorkerError"}]}
        session.commit()
        original_request = gpt_fallback.build_request(session, now=now)
        exchange = gpt_fallback_worker.run_worker(
            engine, _answer(original_request, packages=[checked]), send_enabled=True, now=now)
        assert exchange["flight_review_pending"] == 1 and exchange["sent_count"] == 0, exchange
    else:
        import_package(session, checked, now=now)
        blocked = []
        assert asyncio.run(server_cycle.publish_pending(
            engine, enabled=True, blocked=blocked, scheduler_run_id=cycle.id)) == []
        assert blocked == [{"raw_item_id": raw_id, "reason": "flight_linked_review",
                            "parent_message_id": 153}]
    assert calls == []
    run = session.get(EditorialRun, raw_id, populate_existing=True)
    review_time = datetime.now(UTC)
    state = lifecycle_state(session, raw_id, now=review_time)
    assert (state["state"], state["delivery_attempts"]) == ("waiting_fallback", 0)
    assert state["fallback_attempts"] == int(came_from_fallback)
    assert run.delivery_status == "pending" and run.outcome["status"] == "human_review"
    assert session.get(Post, run.post_id).telegram_message_id is None
    sweep = sweep_materials(session, city="belgrade", now=review_time)
    assert raw_id in sweep.fallback_ids and raw_id in sweep.excluded_ids
    session.refresh(cycle)
    assert raw_id in cycle.details["flight_review_ids"]
    assert cycle.details["fallback_required"] and cycle.status == "partial"
    session.commit()
    assert asyncio.run(server_cycle.publish_pending(engine, enabled=True)) == []
    request = gpt_fallback.build_request(session, now=review_time)
    candidate, = request["candidates"]
    ref = candidate["flight_review_parent"]
    assert ref["message_id"] == 153 and ref["raw_item_id"] == preceding.id
    assert ref in request["published_post_refs"]

    def run_answer(*, packages=(), rejected=()):
        payload = _answer(request, packages=packages, rejected=rejected)
        return gpt_fallback_worker.run_worker(engine, payload, now=review_time)

    rejection = {"raw_item_id": raw_id, "category": "transport",
                 "rationale": "Сведения уже опубликованы."}
    assert run_answer(rejected=[{**rejection, "duplicate_of_message_id": 999}])["declined"] == 0
    assert run_answer(packages=[checked])["ready"] == 0
    if resolution == "duplicate":
        result = run_answer(rejected=[{**rejection, "duplicate_of_message_id": 153}])
        assert result["declined"] == 1 and result["unresolved"] == 0
        session.expire_all()
        assert lifecycle_state(session, raw_id, now=review_time)["reason"] == (
            "flight_duplicate_confirmed")
        assert asyncio.run(server_cycle.publish_pending(engine, enabled=True)) == []
        assert calls == []
    else:
        update = _package(candidate, "Air Serbia подтвердила отмену обратного рейса JU270",
                          "Маршрут из Брюсселя тоже затронут отменой. Обновление перевозчика "
                          "касается обратного вылета JU270.", review_time, parent=ref)
        result = run_answer(packages=[update])
        assert result["ready"] == 1 and result["unresolved"] == 0, result
        session.expire_all()
        settled = lifecycle_state(session, raw_id, now=review_time)
        assert (settled["fallback_attempts"], settled["flight_review_attempts"]) == (
            int(came_from_fallback), 1,
        )
        assert asyncio.run(server_cycle.publish_pending(engine, enabled=True)) == [156]
        replay = run_answer(packages=[update])
        assert replay["ready"] == 0 and replay["obsolete_responses"] == {"already_resolved": 1}
        assert asyncio.run(server_cycle.publish_pending(engine, enabled=True)) == []
        assert calls == [153]
    session.expire_all()
    assert session.get(Post, parent_post.id).telegram_message_id == 153
    assert session.get(EditorialRun, preceding.id).delivery_status == "sent"
