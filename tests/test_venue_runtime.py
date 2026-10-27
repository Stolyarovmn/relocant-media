"""Venue integration checks use public feed fixtures and no AI/network services."""
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.package import EditorialPackage
from relocant_media.collectors import venue_feed
from relocant_media.ops import server_cycle
from relocant_media.sources.registry import ExtractionConfig, load_source_registry
from relocant_media.storage.models import RawItem
from relocant_media.storage.sqlite_schema import initialize_sqlite

NOW = datetime(2026, 10, 20, 20, tzinfo=UTC)
URL = "https://museum.example/event/"
BODY = "Koncert 25. oktobra 2026. u 20 časova u muzeju. " * 8


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    path = tmp_path / 'venue.sqlite3'
    initialize_sqlite(path)
    engine = create_engine(f'sqlite:///{path}')
    original = load_source_registry(Path('cities/belgrade/sources'))[0]
    venue = original.model_copy(update={
        'id': 'test_venue', 'url': 'https://museum.example/feed/',
        'source_type': 'official_venue_events',
        'extraction': ExtractionConfig(strategy='venue_feed', status='active'),
    })
    venues = {venue.id: venue}
    contents = {'body': BODY}
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        requests.append(str(request.url))
        if request.url.host == 'museum.example':
            return httpx.Response(200, text=(
                '<rss xmlns:content="http://purl.org/rss/1.0/modules/content/">'
                '<channel><item><title>Koncert</title><link>' + URL + '</link>'
                '<pubDate>Tue, 20 Oct 2026 18:00:00 +0000</pubDate>'
                '<content:encoded><![CDATA[<p>' + contents['body'] + '</p>]]>'
                '</content:encoded></item></channel></rss>'
            ))
        if request.url.host == 'broken.example':
            return httpx.Response(503)
        return httpx.Response(200, text='No current cards')

    monkeypatch.setattr(server_cycle, 'venue_sources', lambda: venues)
    monkeypatch.setattr(server_cycle.httpx, 'AsyncClient', lambda **kwargs: real_client(
        transport=httpx.MockTransport(handle), **kwargs,
    ))
    monkeypatch.setattr(server_cycle, 'parse_beograd_news_list', lambda *_: [])
    monkeypatch.setattr(server_cycle, 'parse_cukarica_news_list', lambda *_: [])

    async def topics(*args, **kwargs):
        return []

    monkeypatch.setattr(server_cycle, 'collect_topics', topics)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(server_cycle, 'datetime', Clock)
    # Feed freshness and readback use the collector clock independently of the
    # runtime. Keep both on the dated fixture, including consumers of this fixture.
    monkeypatch.setattr(venue_feed, 'datetime', Clock)

    def no_ai(*args, **kwargs):
        raise AssertionError('Сбор и предварительный отсев не вызывают ИИ')

    monkeypatch.setattr(server_cycle.ServerAI, 'generate_json', no_ai)
    yield engine, venue, venues, contents, requests
    engine.dispose()


def stored(engine):
    with Session(engine) as session:
        return [EvidenceDocument(source_id=row.source_id, url=row.url, title=row.title,
                                 body=row.body, published_at=row.published_at.replace(tzinfo=UTC))
                for row in session.scalars(select(RawItem))]


@pytest.mark.asyncio
async def test_current_venue_announcement_ingested_without_ai(runtime):
    engine, _, _, _, _ = runtime
    stats = await server_cycle.collect_sources(engine)
    assert stats['venue_sources'] == 1
    assert stats['venue_items'] == 1
    assert stats['venue_deferred'] == 0
    assert stats['raw_items_written'] == 1
    assert stored(engine)[0].body == BODY.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    'Koncert održan 10. oktobra 2026. ' * 8,
    'Najava koncerta bez datuma događaja. ' * 8,
])
async def test_expired_or_undated_venue_is_filtered_before_ai(runtime, body):
    engine, _, _, contents, _ = runtime
    contents['body'] = body
    stats = await server_cycle.collect_sources(engine)
    assert stats['venue_deferred'] == 1
    assert stats['raw_items_written'] == 0
    assert stored(engine) == []


@pytest.mark.asyncio
async def test_failed_venue_does_not_block_other_sources(runtime):
    engine, venue, venues, _, requests = runtime
    bad = venue.model_copy(update={'id': 'broken', 'url': 'https://broken.example/feed/'})
    venues.clear()
    venues.update({bad.id: bad, venue.id: venue})
    stats = await server_cycle.collect_sources(engine)
    assert stats['venue_sources'] == 2
    assert stats['venue_items'] == 1
    assert stats['raw_items_written'] == 1
    assert stats['failures'] >= 1
    assert any('beograd.rs' in url for url in requests)
    assert any('museum.example' in url for url in requests)


def package(document):
    return EditorialPackage.model_construct(event=SimpleNamespace(documents=(document,)))


@pytest.mark.asyncio
async def test_canonical_venue_evidence_matches_live_readback(runtime):
    import asyncio

    engine, _, _, _, _ = runtime
    await server_cycle.collect_sources(engine)
    documents = stored(engine)
    live = await asyncio.to_thread(server_cycle.fetch_documents, package(documents[0]))
    assert live == tuple(documents)


@pytest.mark.parametrize('source_id,url', [
    ('unknown_source', 'https://museum.example/event/'),
    ('test_venue', 'https://127.0.0.1/private'),
    ('test_venue', 'https://museum.example.evil.test/event/'),
])
def test_unknown_source_or_private_url_fails_closed(runtime, source_id, url):
    _, _, _, _, requests = runtime
    document = EvidenceDocument(source_id=source_id, url=url, title='Koncert', body=BODY,
                                published_at=NOW)
    with pytest.raises(ValueError):
        server_cycle.fetch_documents(package(document))
    assert requests == []
