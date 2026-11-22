import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from relocant_media.ops.source_silence import update_source_silence

START = datetime(2026, 11, 1, 12, tzinfo=UTC)


def _quiet(source_id: str = "bvk_outage_news", **changes: Any) -> dict[str, Any]:
    return {
        "id": source_id,
        "url": "https://example.test/notices",
        "strategy": "wp_posts",
        "status": "empty",
        "empty_reason": "empty_index",
        "item_failures": [],
        **changes,
    }


@pytest.mark.parametrize(("source_id", "days"), [
    ("media_aero_telegraf", 7),
    ("bvk_outage_news", 14),
])
def test_warns_only_at_source_threshold(tmp_path, source_id, days):
    path = tmp_path / "state.json"
    for delay in range(days + 1):
        row = _quiet(source_id)
        assert update_source_silence([row], path, START + timedelta(days=delay))
        assert row["silence_since"] == START.isoformat()
        assert row["silence_hours"] == delay * 24
        assert row["silence_warning_days"] == days
        assert row["status"] == ("empty" if delay < days else "partial")
    assert row["health_reason"] == "prolonged_silence"
    assert row["empty_reason"] == "empty_index"
    history = json.loads(path.read_text(encoding="utf-8"))
    assert set(history["sources"][source_id]) == {"signature", "since", "checked_at"}


@pytest.mark.parametrize("changes", [
    {"status": "ok"},
    {"status": "partial"},
    {"status": "error"},
    {"status": "transport_error"},
    {"empty_reason": "publication_date_unknown"},
    {"item_failures": [{"error_type": "ValueError"}]},
])
def test_a_fault_or_new_item_breaks_continuity(tmp_path, changes):
    path = tmp_path / "state.json"
    assert update_source_silence([_quiet()], path, START)
    interrupted = _quiet(**changes)
    assert update_source_silence([interrupted], path, START + timedelta(hours=20))
    assert "silence_since" not in interrupted
    current = _quiet()
    later = START + timedelta(hours=24)
    assert update_source_silence([current], path, later)
    assert current["silence_since"] == later.isoformat()


@pytest.mark.parametrize(("hours", "changes"), [
    (37, {}),
    (-1, {}),
    (24, {"url": "https://example.test/new"}),
    (24, {"strategy": "rss"}),
])
def test_gap_or_collector_change_starts_a_new_observation(tmp_path, hours, changes):
    path = tmp_path / "state.json"
    assert update_source_silence([_quiet()], path, START)
    row = _quiet(**changes)
    later = START + timedelta(hours=hours)
    assert update_source_silence([row], path, later)
    assert row["silence_since"] == later.isoformat()


def test_corrupt_history_can_be_replaced(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"not-json")
    row = _quiet()
    assert update_source_silence([row], path, START)
    assert row["silence_hours"] == 0
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
