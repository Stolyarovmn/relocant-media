import copy
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_primary_cycle import NOW, ingest_ok
from test_primary_cycle import editorial_session as _editorial_session
from test_primary_cycle import queue as _queue

from relocant_media.ai.batch_preselection import preselect_candidates
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.ops.primary_cycle import run_cycle
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.storage.models import EditorialRun, RawItem

editorial_session = _editorial_session
queue = _queue
PROFILE = load_city_profile("belgrade")


class Provider:
    def __init__(self):
        self.calls = []

    def generate_json(self, **kwargs):
        self.calls.append(kwargs)
        candidates = json.loads(kwargs["prompt"])["candidates"]
        return json.dumps({"ranking": [
            {"raw_item_id": row["raw_item_id"], "reason": "Интересная городская история"}
            for row in reversed(candidates)
        ]})


def raw_ids(engine):
    with Session(engine) as session:
        return list(session.scalars(select(RawItem.id).order_by(RawItem.id)))


def reviewed_ids(engine):
    with Session(engine) as session:
        return set(session.scalars(select(EditorialRun.raw_item_id)))


def test_batch_scans_thirty_but_only_five_receive_full_decisions(queue, tmp_path):
    engine, process = queue
    with Session(engine) as session:
        source_id = session.scalar(select(RawItem.source_id).limit(1))
        for index in range(12, 35):
            session.add(RawItem(
                source_id=source_id, external_id=f"batch-{index}",
                url=f"https://example.test/batch-{index}",
                title=f"Distinct material {index}", body=f"Independent city story {index}",
                published_at=NOW - timedelta(minutes=30), published_at_precision="instant",
            ))
        session.commit()
    provider = Provider()
    scanned = []

    def preselect(candidates, limit):
        scanned.extend(candidate["raw_item_id"] for candidate in candidates)
        return preselect_candidates(candidates, provider, profile=PROFILE, limit=limit, now=NOW)

    report = run_cycle(engine, city="belgrade", output=tmp_path / "batch", now=NOW,
                       ingest=ingest_ok, process=process, preselect=preselect)
    assert len(scanned) == 30 and len(provider.calls) == 1
    assert len(report["processed_raw_item_ids"]) == 5
    assert report["processed_raw_item_ids"] == list(reversed(scanned))[:5]
    assert reviewed_ids(engine) == set(report["processed_raw_item_ids"])
    assert len(set(raw_ids(engine)) - reviewed_ids(engine)) == 30
    assert report["batch_preselection"]["scanned_count"] == 30
    assert report["batch_preselection"]["full_review_ids"] == report["processed_raw_item_ids"]
    assert report["status"] == "PRIMARY_PARTIAL" and report["remaining_count"] == 30


def test_batch_does_not_hide_priority_tip_or_official_transport_disruption(queue, tmp_path):
    engine, process = queue
    ids = raw_ids(engine)
    warning, private_tip = ids[:2]
    with Session(engine) as session:
        item = session.get(RawItem, warning)
        item.title = "Obustava saobraćaja u Geteovoj ulici"
        item.body = "Obustava je privremena, vozila koriste objavljeni obilazak."
        session.commit()
    calls = []

    def choose_other_topics(candidates, limit):
        calls.append(([row["raw_item_id"] for row in candidates], limit))
        return list(reversed(candidates))[:limit], {"status": "ranked", "selected_ids": []}

    report = run_cycle(
        engine, city="belgrade", output=tmp_path / "protected", now=NOW,
        ingest=ingest_ok, process=process, newest_first=True,
        priority_items=lambda: {private_tip}, preselect=choose_other_topics,
    )
    assert len(calls) == 1 and calls[0][1] == 3
    assert not {warning, private_tip} & set(calls[0][0])
    assert set(report["batch_preselection"]["protected_ids"]) == {warning, private_tip}
    assert set(report["processed_raw_item_ids"][:2]) == {warning, private_tip}
    assert len(reviewed_ids(engine)) == 5


def test_mandatory_reassessment_reserves_one_of_five_slots_after_batch(queue, tmp_path):
    engine, process = queue
    held_id = raw_ids(engine)[-1]
    with Session(engine) as session:
        session.add(EditorialRun(
            raw_item_id=held_id, outcome={"status": "human_review", "reason": "technical_hold"},
            evidence={}, delivery_status="pending",
        ))
        session.commit()
    before = reviewed_ids(engine)
    batch_selected = []

    def preselect(candidates, limit):
        selected = candidates[:limit]
        batch_selected.extend(row["raw_item_id"] for row in selected)
        return selected, {"status": "ranked", "selected_ids": list(batch_selected)}

    def reassess_or_process(raw_id):
        if raw_id != held_id:
            return process(raw_id)
        with Session(engine) as session:
            run = session.get(EditorialRun, held_id)
            run.outcome = {"status": "skipped", "reason": "reviewed_again"}
            session.commit()
        return {"raw_item_id": held_id, "transport": "fake"}

    report = run_cycle(
        engine, city="belgrade", output=tmp_path / "repair", now=NOW,
        ingest=ingest_ok, process=reassess_or_process, preselect=preselect,
        reassessment_ids=lambda: ([held_id], []),
    )
    processed = report["processed_raw_item_ids"]
    assert len(processed) == 5 and processed[0] == held_id
    assert processed[1:] == batch_selected[:4]
    assert batch_selected[4] not in reviewed_ids(engine)
    assert len(reviewed_ids(engine) - before) == 4
    assert report["batch_preselection"]["full_review_ids"] == processed
    manifest = json.loads((tmp_path / "repair/manifest.json").read_text(encoding="utf-8"))
    assert manifest["candidates"][0] == {"raw_item_id": held_id, "reassessment": True}


def test_gemini_failure_checkpoints_whole_public_pool_for_gpt(queue, tmp_path):
    engine, process = queue
    protected_id, *_, held_id = raw_ids(engine)
    with Session(engine) as session:
        session.add(EditorialRun(
            raw_item_id=held_id, outcome={"status": "human_review", "reason": "technical_hold"},
            evidence={}, delivery_status="pending",
        ))
        session.commit()

    class Outage(Provider):
        def generate_json(self, **kwargs):
            self.calls.append(kwargs)
            raise TimeoutError("Повреждённый ответ")

    provider = Outage()

    def preselect(candidates, limit):
        return preselect_candidates(candidates, provider, profile=PROFILE, limit=limit, now=NOW)

    report = run_cycle(
        engine, city="belgrade", output=tmp_path / "waiting", now=NOW,
        ingest=ingest_ok, process=process, preselect=preselect,
        defer_failed_batch=True, priority_items=lambda: {protected_id},
        reassessment_ids=lambda: ([held_id], []),
    )
    assert len(provider.calls) == 1
    prompt = json.loads(provider.calls[0]["prompt"])
    submitted_ids = [row["raw_item_id"] for row in prompt["candidates"]]
    assert len(submitted_ids) == 10
    assert report["status"] == "PRIMARY_PARTIAL" and report["fallback_required"] is True
    assert report["window_complete"] is False
    assert report["processed_raw_item_ids"] == [protected_id]
    assert reviewed_ids(engine) == {protected_id, held_id}
    batch = report["batch_preselection"]
    assert batch["status"] == "gpt_waiting" and batch["gpt_slots"] == 4
    assert batch["gpt_candidate_ids"] == submitted_ids
    assert batch["full_review_ids"] == [protected_id] and batch["selected_ids"] == []
    assert report["gpt_deferred_count"] == len(submitted_ids)
    with Session(engine) as session:
        run = session.get(SchedulerRun, report["run_id"])
        assert run.status == "partial" and run.details["batch_preselection"] == batch
        assert run.details["fallback_required"] is True
        assert session.get(EditorialRun, held_id).outcome["status"] == "human_review"


def test_gemini_failure_without_gpt_keeps_local_fallback(queue, tmp_path):
    engine, process = queue

    def preselect(candidates, limit):
        return candidates[:limit], {
            "status": "fallback", "error_type": "GeminiCallTimeout",
            "deferred_ids": [row["raw_item_id"] for row in candidates[limit:]],
        }

    report = run_cycle(
        engine, city="belgrade", output=tmp_path / "local-fallback", now=NOW,
        ingest=ingest_ok, process=process, preselect=preselect,
    )
    assert report["status"] == "PRIMARY_PARTIAL"
    assert report["batch_preselection"]["status"] == "fallback"
    assert len(report["processed_raw_item_ids"]) == 5
    assert reviewed_ids(engine) == set(report["processed_raw_item_ids"])


@pytest.mark.parametrize("invalid", [
    "duplicate", "too_many", "foreign_id", "changed_evidence", "copied_object", "boolean_id",
    "exception",
])
def test_invalid_batch_callback_fails_before_full_processing(queue, tmp_path, invalid):
    engine, process = queue
    processed = []

    def invalid_callback(candidates, limit):
        first = candidates[0]
        if invalid == "exception":
            raise RuntimeError("PRIVATE-API-TOKEN-MUST-NOT-APPEAR")
        if invalid == "duplicate":
            selected = [first, first]
        elif invalid == "too_many":
            selected = candidates[:limit + 1]
        elif invalid == "copied_object":
            selected = [copy.deepcopy(first)]
        else:
            altered = copy.deepcopy(first)
            if invalid == "foreign_id":
                altered["raw_item_id"] = 999999
            elif invalid == "changed_evidence":
                altered["evidence"]["body"] = "Подменённый материал"
            else:
                # При нестрогом равенстве True совпадает с первым integer ID=1.
                altered["raw_item_id"] = True
            selected = [altered]
        return selected, {"status": "ranked"}

    def full_review(raw_id):
        processed.append(raw_id)
        return process(raw_id)

    report = run_cycle(engine, city="belgrade", output=tmp_path / invalid, now=NOW,
                       ingest=ingest_ok, process=full_review, preselect=invalid_callback)
    assert report["status"] == "PRIMARY_FAILED" and not report["window_complete"]
    assert report["error_type"] in {"ValueError", "RuntimeError"}
    assert processed == [] and reviewed_ids(engine) == set()
    assert len(raw_ids(engine)) == 12
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(EditorialRun)) == 0
    assert "PRIVATE-API-TOKEN" not in (tmp_path / invalid / "heartbeat.json").read_text(
        encoding="utf-8",
    )
