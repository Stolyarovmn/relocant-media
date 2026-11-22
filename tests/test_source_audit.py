import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from relocant_media.ops import source_audit
from relocant_media.ops.source_audit import (
    render_source_health_markdown,
    source_health_exit_code,
)


def _result(
    *,
    active: int = 1,
    errors: int = 0,
    partial: int = 0,
    transport_errors: int = 0,
) -> dict[str, Any]:
    return {
        "checked_at": "2026-10-21T22:06:15+00:00",
        "active_count": active,
        "working_count": max(active - errors - transport_errors, 0),
        "error_count": errors,
        "partial_count": partial,
        "transport_error_count": transport_errors,
        "sources": [],
    }


def test_source_health_exit_code_accepts_healthy_registry() -> None:
    assert (
        source_health_exit_code(
            _result(),
            fail_on_error=True,
            fail_on_partial=True,
            fail_on_transport=True,
            require_active=True,
        )
        == 0
    )


def test_source_health_exit_code_rejects_live_errors() -> None:
    assert source_health_exit_code(_result(errors=1), fail_on_error=True) == 1


def test_source_health_exit_code_can_reject_partial_extraction() -> None:
    assert source_health_exit_code(_result(partial=1), fail_on_partial=True) == 1


def test_source_health_exit_code_separates_transport_policy() -> None:
    result = _result(transport_errors=1)

    assert source_health_exit_code(result, fail_on_error=True) == 0
    assert source_health_exit_code(result, fail_on_transport=True) == 1


def test_source_health_exit_code_rejects_empty_active_registry_when_required() -> None:
    assert source_health_exit_code(_result(active=0), require_active=True) == 1


def test_source_health_exit_code_keeps_non_strict_cli_compatible() -> None:
    assert (
        source_health_exit_code(
            _result(active=0, errors=2, partial=3, transport_errors=1)
        )
        == 0
    )


def test_render_source_health_markdown_exposes_degraded_sources() -> None:
    result = _result(active=4, errors=1, partial=1, transport_errors=1)
    result["sources"] = [
        {"id": "good", "status": "ok", "fresh_items": 2, "attempts": 1},
        {
            "id": "partial",
            "status": "partial",
            "fresh_items": 1,
            "item_failures": [{"url": "https://example.test/a", "error_type": "ValueError"}],
        },
        {
            "id": "network",
            "status": "transport_error",
            "error_type": "ConnectError",
            "attempts": 3,
        },
        {"id": "broken", "status": "error", "error_type": "ValueError"},
        {"id": "future", "status": "planned"},
    ]

    rendered = render_source_health_markdown(result)

    assert "partial: **1**" in rendered
    assert "transport errors: **1**" in rendered
    assert "extraction/protocol errors: **1**" in rendered
    assert "| partial | partial | 1 | item_failures=1 |" in rendered
    assert "| network | transport_error | - | ConnectError after 3 attempts |" in rendered
    assert "| broken | error | - | ValueError |" in rendered
    assert "future" not in rendered


def _audit_with_response(monkeypatch, source_id, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(source_audit.httpx, "AsyncClient", lambda **kwargs: client(
        transport=httpx.MockTransport(handler), **kwargs,
    ))
    report = asyncio.run(source_audit.audit_sources(
        Path("cities/belgrade/sources"), online=True, source_ids=(source_id,),
    ))
    return report["sources"][0]


@pytest.mark.parametrize(("scenario", "status"), [
    ("index_changed", "error"), ("article_changed", "error"), ("old_article", "empty"),
])
def test_aviation_health_distinguishes_broken_and_old_articles(monkeypatch, scenario, status):
    article = "https://aero.telegraf.rs/avio-kompanije/123456-air-serbia"
    date = (datetime.now(UTC) - timedelta(days=4)).isoformat()

    def handler(request):
        if request.url.path == "/avio-kompanije":
            index = ("<main>Promenjena struktura</main>" if scenario == "index_changed"
                     else f'<a href="{article}">Air Serbia</a>')
            return httpx.Response(200, text=index)
        assert str(request.url) == article
        if scenario == "article_changed":
            return httpx.Response(200, text="<article><h2>Air Serbia</h2></article>")
        return httpx.Response(200, text=(
            f'<link rel="canonical" href="{article}">'
            f'<meta property="article:published_time" content="{date}">'
            '<article class="single-article"><h1>Air Serbia</h1><div class="article-content">'
            f'<p>{"Saopštenje o letovima sa beogradskog aerodroma. " * 4}</p></div></article>'
        ))

    row = _audit_with_response(monkeypatch, "media_aero_telegraf", handler)
    assert row["status"] == status
    if scenario == "article_changed":
        assert row["item_failures"][0]["url"] == article
    if scenario == "old_article":
        assert row["empty_reason"] == "empty_fresh"


@pytest.mark.parametrize(("scenario", "status"), [
    ("no_notices", "empty"), ("all_broken", "error"), ("one_broken", "partial"),
])
def test_water_source_keeps_valid_notices_and_reports_errors(monkeypatch, scenario, status):
    stamp = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    headline = "Планирани радови на водоводној мрежи"
    base = "https://www.bvk.rs/"

    def document(identity, slug):
        return {"id": identity, "slug": slug, "link": f"{base}{slug}/",
                "title": {"rendered": headline}, "date_gmt": stamp}

    broken = document(123456, "broken")
    valid = document(123457, "valid")

    def handler(request):
        if request.url.path == "/wp-json/wp/v2/posts":
            rows = {"no_notices": [], "all_broken": [broken], "one_broken": [broken, valid]}
            return httpx.Response(200, json=rows[scenario])
        if request.url.path == "/broken/":
            return httpx.Response(200, text="<main>Promenjena struktura</main>")
        assert str(request.url) == valid["link"]
        return httpx.Response(200, text=(
            f'<link rel="canonical" href="{valid["link"]}">'
            f'<article class="post-entry-123457"><h1>{headline}</h1><div class="entry-content">'
            f'<p>{"Без воде ће бити потрошачи у овој улици. " * 3}</p></div></article>'
        ))

    row = _audit_with_response(monkeypatch, "bvk_outage_news", handler)
    assert row["status"] == status
    if scenario == "one_broken":
        assert row["fetched_items"] == 1
    if scenario != "no_notices":
        assert [(failure["url"], failure["error_type"]) for failure in row["item_failures"]] == [
            (broken["link"], "ValueError"),
        ]
    else:
        assert row["empty_reason"] == "empty_index"


@pytest.mark.parametrize(("row_html", "status"), [
    ("", "empty"),
    ("<tr><td>Земун</td><td>08:30 - 14:30</td><td>Улица 5</td></tr>", "empty"),
    ("<tr><td>Земун</td><td>08:30 - 14:30</td><td></td></tr>", "error"),
    ("<tr><td>Земун</td><td>08:30 - 14:30</td><td>Улица 5</td></tr>"
     "<tr><td>Врачар</td><td>?</td><td>Улица 3</td></tr>", "partial"),
])
def test_eds_health_distinguishes_empty_table_from_dropped_rows(monkeypatch, row_html, status):
    def handler(request):
        html = (
            "Планирана искључења за датум: 2026-11-24"
            "<table><tr><td>Navigation</td></tr></table>"
            "<table><tr><th>Општина</th><th>Време</th><th>Улице</th></tr>"
            f"{row_html}</table>"
        )
        return httpx.Response(200, text=html, request=request)

    row = _audit_with_response(monkeypatch, "eds_belgrade_outages_today", handler)
    assert row["status"] == status
    if status == "empty":
        assert row["empty_reason"] == ("publication_date_unknown" if row_html else "empty_index")
        assert row["item_failures"] == []
    else:
        assert len(row["item_failures"]) == 1
        assert row["item_failures"][0]["url"] == row["url"]
        assert row["item_failures"][0]["error_message"].startswith("invalid EDS schedule row ")
    if status == "partial":
        assert row["fetched_items"] == 1


@pytest.mark.parametrize(("second_body", "status"), [
    ("", "error"),
    ("Због планираних радова на водоводу потрошачи ће остати без воде.", "partial"),
])
def test_bvk_planned_works_health_reports_missing_notice_body(monkeypatch, second_body, status):
    def handler(request):
        html = (
            "<h1>Планирани радови</h1>"
            "<h2>24.11.2026. Планирани радови у Вождовцу</h2>"
            "<h2>24.11.2026. Планирани радови на Врачару</h2>"
            f"<p>{second_body}</p><h3>Инфо</h3>"
        )
        return httpx.Response(200, text=html, request=request)

    row = _audit_with_response(monkeypatch, "bvk_planned_works", handler)
    assert row["status"] == status
    assert len(row["item_failures"]) == (1 if second_body else 2)
    if status == "partial":
        assert row["fetched_items"] == 1
