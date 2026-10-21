import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.collectors.telegram_topics import TopicLead
from relocant_media.ops import server_cycle
from relocant_media.storage.models import RawItem
from relocant_media.storage.sqlite_schema import initialize_sqlite

NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)
BEOGRAD = "https://www.beograd.rs/lat/beoinfo-vesti/a116299/radovi.html"
CUKARICA = "https://www.cukarica.rs/vest/123-radovi/"


@pytest.fixture
def collection(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite3"
    initialize_sqlite(path)
    engine = create_engine(f"sqlite:///{path}")
    cards, articles, requests, channel_results = [], {}, [], {}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(server_cycle, "datetime", Clock)
    monkeypatch.setattr(server_cycle, "venue_sources", lambda: {})
    monkeypatch.setattr(server_cycle, "parse_beograd_news_list", lambda *_: cards)
    monkeypatch.setattr(server_cycle, "parse_cukarica_news_list", lambda *_: [])
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(str(request.url))
        return httpx.Response(200, text="Публичная страница")

    monkeypatch.setattr(server_cycle.httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs
    ))

    class Collector:
        def __init__(self, source, client):
            self.client = client

        async def collect_url(self, url):
            await self.client.get(url, follow_redirects=False)
            return articles[url]

    monkeypatch.setattr(server_cycle, "BelgradeArticleCollector", Collector)
    monkeypatch.setattr(server_cycle, "CukaricaNewsCollector", Collector)

    async def topics(channel, client, **kwargs):
        value = channel_results.get(channel, [])
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(server_cycle, "collect_topics", topics)

    def no_ai(*args, **kwargs):
        raise AssertionError("Сбор и отсев не должны вызывать ИИ")

    monkeypatch.setattr(server_cycle.ServerAI, "generate_json", no_ai)
    yield engine, cards, articles, requests, channel_results
    engine.dispose()


def item(age, *, url=BEOGRAD, body="Самостоятельный первоисточник"):
    return CollectedItem(
        source_id="cukarica_news" if url == CUKARICA else "city_beograd_news",
        external_id="123" if url == CUKARICA else "116299", url=url,
        title="Изменение городской инфраструктуры", body=body,
        published_at=NOW - age,
    )


def rows(engine):
    with Session(engine) as session:
        return [(r.source_id, r.url, r.body) for r in session.scalars(select(RawItem))]


@pytest.mark.asyncio
async def test_ten_day_old_card_never_fetches_full_article_or_spends_ai(collection, tmp_path):
    engine, cards, _, requests, _ = collection
    cards.append(item(timedelta(days=10), body="Спектакль уже завершился"))
    result = await server_cycle.collect_sources(engine, output=tmp_path)
    assert BEOGRAD not in requests
    assert rows(engine) == []
    assert result["discarded_before_article_fetch"] == 2  # Две городские ленты.
    assert result["raw_items_written"] == 0
    triage = json.loads((tmp_path / "triage.json").read_text(encoding="utf-8"))
    assert triage["ai_calls"] == 0
    assert "Спектакль" not in json.dumps(triage, ensure_ascii=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("age,expected", [(timedelta(hours=1), 1), (timedelta(days=3), 0)])
async def test_article_freshness_before_persistence_without_ai(collection, age, expected):
    engine, cards, articles, requests, _ = collection
    article = item(age)
    cards.append(article)
    articles[BEOGRAD] = article
    result = await server_cycle.collect_sources(engine)
    assert requests.count(BEOGRAD) == 1
    assert result["raw_items_written"] == expected
    assert len(rows(engine)) == expected
    assert result["discarded_before_persistence"] == 1 - expected


@pytest.mark.asyncio
async def test_channel_text_is_never_evidence_only_allowlisted_primary_links(collection, tmp_path):
    engine, _, articles, requests, channels = collection
    articles.update({BEOGRAD: item(timedelta(hours=1)),
                     CUKARICA: item(timedelta(hours=1), url=CUKARICA)})
    arbitrary = ("https://127.0.0.1/private", "https://evil.test/article",
                 "https://beograd.rs.evil.test/lat/beoinfo-vesti/a116299/radovi.html",
                 "http://www.beograd.rs/lat/beoinfo-vesti/a116299/radovi.html")
    channels[server_cycle.DEFAULT_CHANNELS[0]] = [TopicLead(
        channel=server_cycle.DEFAULT_CHANNELS[0], message_id=10,
        url="https://t.me/beogradske_vesti/10", published_at=NOW,
        observed_at=NOW, text="УНИКАЛЬНЫЙ ТЕКСТ КАНАЛА", views=None,
        outbound_urls=(BEOGRAD, CUKARICA, *arbitrary),
    )]
    result = await server_cycle.collect_sources(engine, output=tmp_path)
    assert result["raw_items_written"] == 2
    assert {r[1] for r in rows(engine)} == {BEOGRAD, CUKARICA}
    assert all(r[2] == "Самостоятельный первоисточник" for r in rows(engine))
    assert not set(arbitrary) & set(requests)
    audit = (tmp_path / "channel-topics.json").read_text(encoding="utf-8")
    assert "УНИКАЛЬНЫЙ ТЕКСТ КАНАЛА" not in audit
    assert '"body"' not in audit and '"text"' not in audit
    assert len(audit) < 2000
    assert json.loads(audit)["publication_evidence"] is False


@pytest.mark.asyncio
async def test_unavailable_channel_is_isolated_and_missing_primary_uses_no_ai(collection, tmp_path):
    engine, _, _, _, channels = collection
    channels[server_cycle.DEFAULT_CHANNELS[0]] = httpx.ReadTimeout("Недоступен")
    channels[server_cycle.DEFAULT_CHANNELS[1]] = [TopicLead(
        channel=server_cycle.DEFAULT_CHANNELS[1], message_id=1,
        url="https://t.me/sta_imas_beograd/1", published_at=NOW,
        observed_at=NOW, text="Непроверенная тема", outbound_urls=(), views=None,
    )]
    result = await server_cycle.collect_sources(engine, output=tmp_path)
    assert result["channel_failures"] == 1
    assert result["channel_leads"] == 1
    assert rows(engine) == []
    audit = json.loads((tmp_path / "channel-topics.json").read_text(encoding="utf-8"))
    assert audit["topics"][0]["status"] == "deferred_missing_primary_source"
    assert audit["topics"][0]["ai_calls"] == 0
    assert audit["failures"][0]["error_type"] == "ReadTimeout"


@pytest.mark.asyncio
async def test_repeated_channel_link_to_stale_primary_is_never_reported_resolved(
    collection, tmp_path,
):
    engine, _, articles, _, channels = collection
    articles[BEOGRAD] = item(timedelta(days=3))
    for channel in server_cycle.DEFAULT_CHANNELS[:2]:
        channels[channel] = [TopicLead(
            channel=channel, message_id=1, url=f"https://t.me/{channel}/1",
            published_at=NOW, observed_at=NOW, text="Непроверенная тема", views=None,
            outbound_urls=(BEOGRAD,),
        )]
    await server_cycle.collect_sources(engine, output=tmp_path)
    assert rows(engine) == []
    audit = json.loads((tmp_path / "channel-topics.json").read_text(encoding="utf-8"))
    assert len(audit["topics"]) == 2
    assert all(t["status"] == "deferred_missing_primary_source" for t in audit["topics"])
