from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "src/relocant_media/ops/server_cycle.py"
TESTS = ROOT / "tests/test_server_cycle.py"
CI = ROOT / ".github/workflows/ci.yml"
SELF = Path(__file__).resolve()
BRANCH = "feat/structured-venue-server-cycle"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {text.count(old)}")
    return text.replace(old, new, 1)


server = SERVER.read_text(encoding="utf-8")
server = replace_once(
    server,
    "from relocant_media.collectors.news_discovery import (",
    "from relocant_media.collectors.factory import build_collector\n"
    "from relocant_media.collectors.news_discovery import (",
    "factory import",
)
server = replace_once(
    server,
    'NEWS_DISCOVERY = Path("docs/serbian-news-discovery.json")\n',
    'NEWS_DISCOVERY = Path("docs/serbian-news-discovery.json")\n'
    '_SERVER_VENUE_STRATEGIES = frozenset({\n'
    '    "venue_feed",\n'
    '    "musicology_blog",\n'
    '    "natural_history_news",\n'
    '})\n',
    "server venue strategies",
)

pattern = re.compile(
    r"def venue_sources\(\) -> dict\[str, SourceConfig\]:\n"
    r".*?\n\n"
    r"def fetch_documents\(package: EditorialPackage\) -> tuple\[EvidenceDocument, \.\.\.\]:",
    re.DOTALL,
)
replacement = '''def venue_sources() -> dict[str, SourceConfig]:
    """Active venue sources safe for the dated autonomous editorial queue."""
    return {
        source.id: source
        for source in load_source_registry(SOURCES)
        if source.extraction.strategy in _SERVER_VENUE_STRATEGIES
        and source.extraction.status == "active"
        and source.source_type == "official_venue_events"
    }


def discovery_venue_sources() -> dict[str, SourceConfig]:
    """Legacy search-enabled venue feeds; structured list collectors are not search APIs."""
    return {
        source.id: source
        for source in load_source_registry(SOURCES)
        if source.extraction.strategy == "venue_feed"
        and source.extraction.status == "active"
        and source.source_type == "official_venue_events"
    }


async def _collect_exact_url(collector: Any, url: str) -> Any:
    collect_url = getattr(collector, "collect_url", None)
    if callable(collect_url):
        return await collect_url(url)
    matches = [
        item
        for item in await collector.collect()
        if item.url == url or item.canonical_url == url
    ]
    if len(matches) != 1:
        raise ValueError("official source item unavailable or ambiguous")
    return matches[0]


def fetch_documents(package: EditorialPackage) -> tuple[EvidenceDocument, ...]:'''
server, count = pattern.subn(replacement, server, count=1)
if count != 1:
    raise RuntimeError(f"venue source block: expected one replacement, got {count}")

server = replace_once(
    server,
    '''    venues = venue_sources()\n    async def fetch_venue(source: SourceConfig, url: str) -> Any:\n        async with httpx.AsyncClient(timeout=25, trust_env=False) as venue_client:\n            return await VenueFeedCollector(source, venue_client).collect_url(url)\n''',
    '''    venues = venue_sources()\n    async def fetch_venue(source: SourceConfig, url: str) -> Any:\n        async with httpx.AsyncClient(timeout=25, trust_env=False) as venue_client:\n            collector = build_collector(source, venue_client)\n            return await _collect_exact_url(collector, url)\n''',
    "fetch_documents venue refresher",
)
server = replace_once(
    server,
    '''    venues = venue_sources()\n    news_feeds = () if submissions_only else load_verified_news_feeds(NEWS_DISCOVERY)\n''',
    '''    venues = venue_sources()\n    discovery_venues = discovery_venue_sources()\n    news_feeds = () if submissions_only else load_verified_news_feeds(NEWS_DISCOVERY)\n''',
    "collect source sets",
)
server = replace_once(
    server,
    '''        collectors: dict[\n            str, BelgradeArticleCollector | CukaricaNewsCollector | VenueFeedCollector\n        ] = {\n''',
    '''        collectors: dict[str, Any] = {\n''',
    "collector dictionary type",
)
server = replace_once(
    server,
    '''        collectors.update({key: VenueFeedCollector(source, client)\n                           for key, source in venues.items()})\n''',
    '''        collectors.update({\n            key: build_collector(source, client) for key, source in venues.items()\n        })\n''',
    "collector factory wiring",
)
server = replace_once(
    server,
    "            item = await collectors[source_id].collect_url(url)\n",
    "            item = await _collect_exact_url(collectors[source_id], url)\n",
    "full article exact refresh",
)
server = replace_once(
    server,
    "            return public_search_plan(text, venues, aliases)\n",
    "            return public_search_plan(text, discovery_venues, aliases)\n",
    "discovery search venue set",
)
server = replace_once(
    server,
    '''            search_source = venues[source_id].model_copy(update={"extraction":\n                venues[source_id].extraction.model_copy(update={"feed_url": search_url})})\n''',
    '''            search_source = discovery_venues[source_id].model_copy(update={"extraction":\n                discovery_venues[source_id].extraction.model_copy(update={"feed_url": search_url})})\n''',
    "discovery search source",
)
server = replace_once(
    server,
    "                current = await collectors[original.source_id].collect_url(original.url)\n",
    "                current = await _collect_exact_url(collectors[original.source_id], original.url)\n",
    "published source exact refresh",
)
server = replace_once(
    server,
    "            collector = VenueFeedCollector(venues[source_id], client)\n",
    "            collector = collectors[source_id]\n",
    "venue collection factory",
)
server = replace_once(
    server,
    '''            finally:\n                result["venue_article_failures"] += len(collector.item_failures)\n                venue_errors.extend({"source_id": source_id, **error}\n                                    for error in collector.item_failures)\n''',
    '''            finally:\n                item_failures = getattr(collector, "item_failures", [])\n                result["venue_article_failures"] += len(item_failures)\n                venue_errors.extend({"source_id": source_id, **error}\n                                    for error in item_failures)\n''',
    "venue item failures",
)
SERVER.write_text(server, encoding="utf-8")

tests = TESTS.read_text(encoding="utf-8")
marker = "def test_runtime_venue_sources_include_dated_structured_collectors():"
if marker in tests:
    raise RuntimeError("runtime venue regression already exists")
tests += '''\n\n\ndef test_runtime_venue_sources_include_dated_structured_collectors():\n    venues = server_cycle.venue_sources()\n    assert "venue_musicology" in venues\n    assert venues["venue_musicology"].extraction.strategy == "musicology_blog"\n    assert "venue_natural_history" in venues\n    assert venues["venue_natural_history"].extraction.strategy == "natural_history_news"\n    assert "venue_bitefartcafe" not in venues\n    assert "venue_yugoslavia" not in venues\n\n    discovery = server_cycle.discovery_venue_sources()\n    assert discovery\n    assert all(source.extraction.strategy == "venue_feed" for source in discovery.values())\n    assert set(discovery) < set(venues)\n\n\n@pytest.mark.asyncio\nasync def test_structured_venue_exact_refresh_requires_one_matching_official_url():\n    target = CollectedItem(\n        source_id="venue_musicology",\n        external_id="https://musicology.rs/2026/10/14/example/",\n        url="https://musicology.rs/2026/10/14/example/",\n        title="Example",\n        body="Verified official body",\n        published_at=datetime(2026, 10, 14, 12, tzinfo=UTC),\n        canonical_url="https://musicology.rs/2026/10/14/example/",\n    )\n\n    class StructuredCollector:\n        async def collect(self):\n            return [target]\n\n    assert await server_cycle._collect_exact_url(\n        StructuredCollector(), target.url\n    ) is target\n\n    class AmbiguousCollector:\n        async def collect(self):\n            return [target, target]\n\n    with pytest.raises(ValueError, match="unavailable or ambiguous"):\n        await server_cycle._collect_exact_url(AmbiguousCollector(), target.url)\n'''
TESTS.write_text(tests, encoding="utf-8")

subprocess.run(["git", "fetch", "origin", "main"], cwd=ROOT, check=True)
main_ci = subprocess.check_output(
    ["git", "show", "origin/main:.github/workflows/ci.yml"], cwd=ROOT, text=True
)
CI.write_text(main_ci, encoding="utf-8")
SELF.unlink()

subprocess.run(["git", "config", "user.name", "IGT Runtime Patch"], cwd=ROOT, check=True)
subprocess.run(
    ["git", "config", "user.email", "runtime-patch@users.noreply.github.com"],
    cwd=ROOT,
    check=True,
)
subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
subprocess.run(
    ["git", "commit", "-m", "Wire dated structured venues into server cycle"],
    cwd=ROOT,
    check=True,
)
subprocess.run(
    ["git", "push", "origin", f"HEAD:refs/heads/{BRANCH}"],
    cwd=ROOT,
    check=True,
)
