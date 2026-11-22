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


def test_rolling_kpi_shown_with_measurement_time_and_not_as_calendar_day():
    value = snapshot()
    value["cities"][0]["kpi"] = {
        "subscribers": {"count": 2, "observed_at": NOW.isoformat(), "delta_24h": 0,
                        "delta_7d": -1, "private": "SECRET"},
        "published": {"last_24h": 3, "last_7d": 28,
                      "last_published_at": "2026-10-26T17:02:03+00:00", "hours_since_last": 5},
    }
    report = build_status(value, city="belgrade", now=NOW)
    kpi = report["rolling_kpi"]
    assert kpi["window"] == "rolling"
    assert (kpi["published_last_24h"], kpi["published_last_7d"], kpi["subscribers"]) == (3, 28, 2)
    assert report["daily_results"]["published_posts"] is None
    text = markdown(report)
    assert "скользящие 24 ч до 27.10 01:00 МСК: 3; за 7 дней: 28" in text
    assert "Подписчики: 2 (27.10 01:00 МСК); за 24 ч +0, за 7 дней -1" in text
    assert "календарные сутки: неизвестно" in text
    assert "SECRET" not in json.dumps(report)


@pytest.mark.parametrize("kpi", [None, "junk", {"published": {"last_24h": True, "last_7d": -2},
                                               "subscribers": {"count": "2", "delta_24h": 1.5}}])
def test_missing_or_invalid_kpi_is_no_data_not_zero(kpi):
    value = snapshot()
    if kpi is not None:
        value["cities"][0]["kpi"] = kpi
    report = build_status(value, city="belgrade", now=NOW)
    kpi_report = report["rolling_kpi"]
    assert kpi_report["published_last_24h"] is None
    assert kpi_report["published_last_7d"] is None
    assert kpi_report["subscribers"] is None
    assert kpi_report["subscribers_delta_24h"] is None
    text = markdown(report)
    assert "24 ч до 27.10 01:00 МСК: нет данных; за 7 дней: нет данных" in text
    assert "Подписчики: нет данных (время неизвестно)" in text


def test_unavailable_city_has_no_kpi():
    report = build_status(snapshot(), city="novi_sad", now=NOW)
    assert report["rolling_kpi"] is None
    assert "Отдельные публикации и подписчики: нет данных." in markdown(report)
