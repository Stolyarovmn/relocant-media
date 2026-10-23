from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.collectors.srbija_gov_article import (
    SrbijaGovArticleCollector,
    SrbijaGovExtractionError,
    match_srbija_gov_article_url,
    parse_srbija_gov_article,
)
from relocant_media.ops import server_runtime
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import RawItem, Source, Submission
from relocant_media.storage.sqlite_schema import initialize_sqlite

ARTICLE = (
    "https://www.srbija.gov.rs/vest/1009119/"
    "bezbednost-izbornog-procesa-apsolutni-prioritet-mup-a.php"
)
NOW = datetime(2026, 10, 23, 10, tzinfo=UTC)
BODY = (
    "Дирекција полиције активно спроводи мере из своје надлежности како би цео "
    "изборни процес протекао безбедно. Руководиоцима организационих јединица "
    "наложено је доследно поштовање закона и професионално поступање."
)
HTML = f"""
<html><body><main><article>
  <div class="date">Београд, 22. октобар 2026.</div>
  <h1>Безбедност изборног процеса апсолутни приоритет МУП-а</h1>
  <p>{BODY}</p>
  <p>Изборни процес заказан је за 25. октобар, што није датум публикације.</p>
</article></main></body></html>
"""


def source():
    return load_source_file(Path("cities/belgrade/sources/srbija_gov_news.yaml"))


def test_exact_government_article_url_is_fail_closed() -> None:
    assert match_srbija_gov_article_url(ARTICLE) is not None
    assert match_srbija_gov_article_url("https://www.srbija.gov.rs/vest/1009119") is not None
    assert match_srbija_gov_article_url(ARTICLE + "?utm_source=x") is None
    assert match_srbija_gov_article_url(ARTICLE + "#fragment") is None
    assert match_srbija_gov_article_url(ARTICLE.replace("https://", "http://")) is None
    assert match_srbija_gov_article_url(ARTICLE.replace("www.srbija.gov.rs", "evil.example")) is None
    assert match_srbija_gov_article_url(
        ARTICLE.replace("www.srbija.gov.rs", "www.srbija.gov.rs:444")
    ) is None


def test_parser_uses_explicit_publication_date_not_event_date() -> None:
    item = parse_srbija_gov_article(HTML, source(), ARTICLE)
    assert item.source_id == "srbija_gov_news"
    assert item.external_id == "1009119"
    assert item.title == "Безбедност изборног процеса апсолутни приоритет МУП-а"
    assert item.published_at is not None
    assert item.published_at.isoformat() == "2026-10-22T00:00:00+02:00"
    assert "25. октобар" in item.body


def test_parser_refuses_missing_explicit_publication_date() -> None:
    html = HTML.replace("Београд, 22. октобар 2026.", "")
    with pytest.raises(SrbijaGovExtractionError, match="publication date"):
        parse_srbija_gov_article(html, source(), ARTICLE)


@pytest.mark.asyncio
async def test_collector_validates_same_article_redirect() -> None:
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path == "/vest/1009119":
            return httpx.Response(302, headers={"location": ARTICLE})
        return httpx.Response(200, text=HTML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        item = await SrbijaGovArticleCollector(source(), client).collect_url(
            "https://www.srbija.gov.rs/vest/1009119"
        )
    assert item.external_id == "1009119"
    assert item.source_html is None
    assert item.approved_image_hosts == ()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_runtime_links_private_tip_to_independently_fetched_official_item(
    tmp_path, monkeypatch,
) -> None:
    path = tmp_path / "editorial.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    with Session(engine) as session:
        session.add(Submission(
            kind="news_tip",
            telegram_user_id=345042757,
            telegram_message_id=77,
            status="new",
            payload={"city": "belgrade", "chat_id": 345042757, "text": ARTICLE},
        ))
        session.commit()

    real_async_client = httpx.AsyncClient

    def async_client(**kwargs):
        return real_async_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=HTML)),
            **kwargs,
        )

    monkeypatch.setattr(server_runtime.httpx, "AsyncClient", async_client)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(server_runtime, "datetime", Clock)

    async def base_collect(engine, *, output=None, submissions_only=False):
        del output, submissions_only
        # Reproduce the base runtime seeing this new URL as unknown. The extension must merge
        # the independently fetched evidence back after the base pass rather than losing it.
        with Session(engine) as session:
            row = session.scalar(select(Submission))
            assert row is not None
            row.status = "waiting_primary"
            row.payload = {**row.payload, "raw_item_ids": [], "triage_reason": "missing_primary"}
            session.commit()
        return {
            "sources_synced": 2,
            "raw_items_written": 0,
            "review_required": 0,
            "priority_submissions": 0,
            "private_link_fetches": 0,
            "submission_raw_ids": [],
        }

    monkeypatch.setattr(server_runtime, "_BASE_COLLECT_SOURCES", base_collect)
    result = await server_runtime.collect_sources(engine, submissions_only=True)

    with Session(engine) as session:
        stored_source = session.get(Source, "srbija_gov_news")
        raw = session.scalar(select(RawItem).where(RawItem.source_id == "srbija_gov_news"))
        submission = session.scalar(select(Submission))
        assert stored_source is not None and stored_source.high_risk is True
        assert stored_source.auto_publish is True
        assert raw is not None and raw.external_id == "1009119"
        assert submission is not None and submission.status == "source_linked"
        assert submission.payload["raw_item_ids"] == [raw.id]

    assert result["private_link_fetches"] == 1
    assert result["priority_submissions"] == 1
    assert result["srbija_gov_direct"]["failures"] == 0
    engine.dispose()


def test_owner_review_live_fetch_uses_same_strict_extractor(monkeypatch) -> None:
    real_client = httpx.Client

    def client(**kwargs):
        return real_client(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, text=HTML)),
            **kwargs,
        )

    monkeypatch.setattr(server_runtime.httpx, "Client", client)
    live = server_runtime._fetch_srbija_document(EvidenceDocument(
        source_id="srbija_gov_news",
        url=ARTICLE,
        title="stale title",
        body="stale body",
        published_at=NOW,
    ))
    assert live.source_id == "srbija_gov_news"
    assert live.title == "Безбедност изборног процеса апсолутни приоритет МУП-а"
    assert live.published_at is not None
    assert live.published_at.isoformat() == "2026-10-22T00:00:00+02:00"
