import copy
import json
from datetime import UTC, datetime

import pytest

from relocant_media.ops.daily_analysis import build_status, markdown

NOW = datetime(2026, 10, 26, 22, tzinfo=UTC)


def snapshot():
    return {"schema_version": 1, "mode": "public_read_only", "cities": [
        {"id": "belgrade", "runtime_status": "available", "observed_at": NOW.isoformat(),
         "counts": {"sent": 17, "held": 24, "uncertain": 1},
         "items": [{"state": "held", "reason_code": "utility_hold"}] * 2},
        {"id": "novi_sad", "runtime_status": "unavailable", "observed_at": None},
    ]}


def test_lifetime_deliveries_never_become_daily_results_and_private_fields_not_copied():
    value = snapshot()
    value["cities"][0]["items"][0]["private_text"] = "SECRET"
    value["cities"][0]["items"].append({"state": "held", "reason_code": "SECRET BODY"})
    original = copy.deepcopy(value)
    report = build_status(value, city="belgrade", now=NOW)
    assert report["snapshot"]["queue_counts"]["sent"] == 17
    assert report["daily_results"]["published_posts"] is None
    assert report["quality"]["repeated_reason_codes"] == [
        {"reason_code": "utility_hold", "occurrences": 2}]
    assert not report["quality"]["trend_confirmed"]
    assert "SECRET" not in json.dumps(report)
    assert value == original
    assert report["report_date"] == "2026-10-27"
    assert 3 <= len(report["next_actions"]) <= 5
    assert any("не повторять" in item for item in report["next_actions"])
    assert "не суточные" in markdown(report)


@pytest.mark.parametrize("observed", ["2026-10-24T12:00:00+00:00", "2027-01-01T00:00:00+00:00"])
def test_old_or_future_observation_stays_stale(observed):
    value = snapshot()
    value["cities"][0]["observed_at"] = observed
    report = build_status(value, city="belgrade", now=NOW)
    assert report["snapshot"]["status"] == "stale"
    assert report["snapshot"]["observed_at"] == observed
    assert "устарели" in report["next_actions"][0]


@pytest.mark.parametrize("value", [None, snapshot()])
def test_city_without_observation_has_unknown_counts_and_signals(value):
    report = build_status(value, city="novi_sad", now=NOW)
    assert report["snapshot"]["status"] == "unavailable"
    assert report["snapshot"]["queue_counts"] is None
    assert all(value is None for value in report["daily_results"].values())
    assert "недоступны" in markdown(report)


def test_invalid_snapshot_and_naive_report_time_rejected():
    with pytest.raises(ValueError, match="public newsroom"):
        build_status({"schema_version": 2}, city="belgrade", now=NOW)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_status(None, city="belgrade", now=datetime(2026, 10, 26))


def test_unknown_count_keys_negative_numbers_and_bools_not_exported():
    value = snapshot()
    value["cities"][0]["counts"] = {"sent": True, "held": -1, "private": 5}
    report = build_status(value, city="belgrade", now=NOW)
    assert report["snapshot"]["queue_counts"] == {}


def test_cli_missing_snapshot_writes_unavailable_artifacts(tmp_path, monkeypatch):
    from relocant_media.ops.daily_analysis import main

    output = tmp_path / "report"
    monkeypatch.setattr("sys.argv", ["daily-analysis", "--snapshot", str(tmp_path / "missing"),
                                     "--output", str(output)])
    main()
    report = json.loads((output / "status.json").read_text())
    assert report["snapshot"]["status"] == "unavailable"
    assert report["daily_results"]["published_posts"] is None
    assert (output / "status.md").is_file()


def test_cli_legacy_empty_novi_sad_is_unavailable(tmp_path, monkeypatch):
    from pathlib import Path

    from relocant_media.ops.daily_analysis import main

    source = tmp_path / "legacy.json"
    source.write_text(json.dumps({"schema_version": 1, "mode": "public_read_only", "cities": [
        {"id": "novi_sad", "runtime_status": "available", "observed_at": NOW.isoformat(),
         "total_events": 0, "counts": {"sent": 0}, "items": [], "runs": [],
         "ai_usage": [], "sources": []},
    ]}))
    output = tmp_path / "report"
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setattr("sys.argv", ["daily-analysis", "--snapshot", str(source),
                                     "--city", "novi_sad", "--root", str(root),
                                     "--output", str(output)])
    main()
    report = json.loads((output / "status.json").read_text())
    assert report["snapshot"]["status"] == "unavailable"
    assert report["snapshot"]["queue_counts"] is None
    assert report["snapshot"]["observed_at"] is None


def test_cli_failed_checkout_ignores_existing_partial_snapshot(tmp_path, monkeypatch):
    from relocant_media.ops.daily_analysis import main

    source = tmp_path / "partially-fetched.json"
    source.write_text("not even valid JSON; must never be read")
    output = tmp_path / "report"
    monkeypatch.setattr("sys.argv", ["daily-analysis", "--snapshot", str(source),
                                     "--snapshot-unavailable", "--output", str(output)])
    main()
    report = json.loads((output / "status.json").read_text())
    assert report["snapshot"]["status"] == "unavailable"
    assert report["snapshot"]["queue_counts"] is None
