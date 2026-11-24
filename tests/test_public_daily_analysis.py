import copy
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from relocant_media.ops.daily_analysis import build_status, markdown, owner_markdown

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


@pytest.mark.parametrize(("observed_hour", "posts", "stale", "expected"), [
    (19, 4, False, False),  # До 21:00 в Белграде.
    (20, 0, False, True),
    (20, 4, False, True),
    (20, 6, False, False),
    (20, None, False, False),
    (20, 4, True, False),
    (23, 4, False, False),  # Срез редакционного потока из будущего.
])
def test_low_release_volume_uses_confirmed_count_after_daytime_window(
    observed_hour, posts, stale, expected,
):
    value = snapshot()
    city = value["cities"][0]
    if stale:
        city["observed_at"] = (NOW - timedelta(hours=7)).isoformat()
    day = NOW.date()
    city["editorial_flow"] = {
        "observed_at": NOW.replace(hour=observed_hour).isoformat(),
        "timezone": "Europe/Belgrade",
        "days": [{"local_date": day.isoformat(), "expired_unreviewed": [],
                  "delivery_latency": [], "publication_cadence": None if posts is None else {
                      "posts": posts, "quiet_hour_posts": 0, "max_daytime_gap_minutes": 780,
                  }}],
    }
    report = build_status(value, city="belgrade", now=NOW, day=day)
    signal = f"Низкий выпуск за {day} — {posts} подтверждённых постов при ориентире 6"
    assert (signal in markdown(report)) is expected
    assert "Низкий выпуск" not in owner_markdown(report)


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
    assert "Рабочий снимок недоступен" in (output / "owner.md").read_text(encoding="utf-8")


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


@pytest.mark.parametrize(("age", "delta_24h", "delta_7d"), [
    ({"hours": 2}, 0, -1),
    ({"hours": 24}, None, -1),
    ({"hours": 30}, None, -1),
    ({"days": 7}, None, None),
    ({"days": 9}, None, None),
])
def test_stale_subscriber_reading_has_no_delta_but_keeps_count(age, delta_24h, delta_7d):
    from datetime import timedelta

    value = snapshot()
    value["cities"][0]["kpi"] = {"subscribers": {
        "count": 2, "observed_at": (NOW - timedelta(**age)).isoformat(),
        "delta_24h": 0, "delta_7d": -1}}
    kpi = build_status(value, city="belgrade", now=NOW)["rolling_kpi"]
    assert kpi["subscribers"] == 2
    assert (kpi["subscribers_delta_24h"], kpi["subscribers_delta_7d"]) == (delta_24h, delta_7d)


def test_unknown_subscriber_time_has_no_delta():
    value = snapshot()
    value["cities"][0]["kpi"] = {"subscribers": {"count": 2, "delta_24h": 0, "delta_7d": 0}}
    report = build_status(value, city="belgrade", now=NOW)
    assert report["rolling_kpi"]["subscribers_delta_24h"] is None
    assert "за 24 ч нет данных, за 7 дней нет данных" in markdown(report)


def test_owner_brief_has_evidence_links_and_clear_scope():
    value = snapshot()
    city = value["cities"][0]
    city["daily_brief"] = {
        "timezone": "Europe/Belgrade", "days": [
            {"local_date": "2026-10-25", "post_count": 0, "message_ids": [], "views": None,
             "measured_posts": 0},
            {"local_date": "2026-10-26", "post_count": 3, "message_ids": [101, 102, 103],
             "views": 0, "measured_posts": 1, "private_text": "SECRET",
             "morning": {"status": "sent", "message_id": 104, "url": "https://example.com/secret"},
             "evening": {"status": "unconfirmed"}},
        ],
    }
    city["kpi"] = {"subscribers": {"count": 2, "observed_at": NOW.isoformat()}}
    city["last_cycle"] = {"observed_at": NOW.isoformat(), "processing_error_count": 2,
                          "fallback_required": True, "detail": "SECRET"}
    value["last_attempt"] = {
        "observed_at": NOW.isoformat(), "status": "failure", "mode": "editorial",
    }
    report = build_status(value, city="belgrade", now=NOW)
    text = owner_markdown(report)
    assert report["report_date"] == "2026-10-27"
    assert report["owner_brief"]["local_date"] == "2026-10-26"
    assert report["owner_brief"]["publications"]["post_count"] == 3
    assert len(text.splitlines()) == 7
    assert "[№101](https://t.me/eduvbelgrad/101)" in text
    assert "[«Белград сегодня»](https://t.me/eduvbelgrad/104)" in text
    assert "«Главное за день» — выход не подтверждён" in text
    assert "Просмотры — 0 по 1 из 3 постов" in text
    assert "завершился ошибкой" in text and "запросил резервную проверку" in text
    assert "Записей для сверки с Telegram — 1. Снимок без разбивки по статусам." in text
    assert "SECRET" not in json.dumps(report) and "example.com" not in text
    assert build_status(value, city="belgrade", now=NOW, day=date(2026, 10, 25))[
        "owner_brief"]["publications"]["post_count"] == 0
    assert build_status(value, city="belgrade", now=NOW + timedelta(days=1))[
        "owner_brief"]["publications"] is None


def test_old_snapshot_stays_unknown_in_a_short_report():
    value = snapshot()
    value["cities"][0]["observed_at"] = (NOW - timedelta(hours=7)).isoformat()
    text = owner_markdown(build_status(value, city="belgrade", now=NOW))
    assert "Снимок устарел" in text
    assert "Отдельных постов за день — нет данных" in text
    assert "Просмотры — нет данных" in text


@pytest.mark.parametrize(("unfinished", "label"), [
    (1, "исход отправки неизвестен — 1 · статус «отправлено» без квитанции — 1"),
    (0, "статус «отправлено» без квитанции — 1"),
])
def test_short_report_separates_live_delivery_from_status_without_receipt(unfinished, label):
    value = snapshot()
    value["cities"][0]["delivery_checks"] = {
        "uncertain_send": unfinished, "sent_without_receipt": 1,
        "sending": 0, "receipt_conflict": 0, "private": "SECRET",
    }
    report = build_status(value, city="belgrade", now=NOW)
    assert owner_markdown(report).splitlines()[-1] == f"Сверка доставки · {label}."
    assert "SECRET" not in json.dumps(report)
