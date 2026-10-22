from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "src/relocant_media/ops/server_cycle.py"
TESTS = ROOT / "tests/test_server_cycle.py"
CI = ROOT / ".github/workflows/ci.yml"
SELF = Path(__file__).resolve()
BRANCH = "feat/undated-venue-review-holds"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected exactly one match, got {count}")
    return text.replace(old, new, 1)


server = SERVER.read_text(encoding="utf-8")
server = replace_once(
    server,
    '''_SERVER_VENUE_STRATEGIES = frozenset({\n    "venue_feed",\n    "musicology_blog",\n    "natural_history_news",\n})\n''',
    '''_SERVER_VENUE_STRATEGIES = frozenset({\n    "venue_feed",\n    "musicology_blog",\n    "natural_history_news",\n})\n_REVIEW_ONLY_VENUE_STRATEGIES = frozenset({\n    "bitefartcafe_events",\n    "yugoslavia_programs",\n})\n''',
    "venue strategy constants",
)
server = replace_once(
    server,
    '''def discovery_venue_sources() -> dict[str, SourceConfig]:\n''',
    '''def review_only_venue_sources() -> dict[str, SourceConfig]:\n    """Verified undated venue sources retained only for deterministic human review."""\n    return {\n        source.id: source\n        for source in load_source_registry(SOURCES)\n        if source.extraction.strategy in _REVIEW_ONLY_VENUE_STRATEGIES\n        and source.extraction.status == "active"\n        and source.source_type == "official_venue_events"\n        and not source.policy.auto_publish\n    }\n\n\ndef _is_review_only_venue_hold(item: Any, *, now: datetime) -> bool:\n    if event_content_reason(item.title, item.body, now=now) is not None:\n        return False\n    fresh = freshness_decision(item.title, item.body, item.published_at, now)\n    return fresh.action == "hold" and fresh.reason == "publication_date_unknown"\n\n\ndef discovery_venue_sources() -> dict[str, SourceConfig]:\n''',
    "review-only venue helpers",
)
server = replace_once(
    server,
    '''    venues = venue_sources()\n    discovery_venues = discovery_venue_sources()\n''',
    '''    venues = venue_sources()\n    review_venues = review_only_venue_sources()\n    runtime_venues = {**venues, **review_venues}\n    discovery_venues = discovery_venue_sources()\n''',
    "venue source sets",
)
server = replace_once(
    server,
    '''    sources.update(venues)\n    result["sources_synced"] = len(sources)\n    result["venue_sources"] = len(venues)\n    result["venue_items"] = 0\n    result["venue_deferred"] = 0\n''',
    '''    sources.update(runtime_venues)\n    result["sources_synced"] = len(sources)\n    result["venue_sources"] = len(runtime_venues)\n    result["venue_review_only_sources"] = len(review_venues)\n    result["venue_items"] = 0\n    result["venue_deferred"] = 0\n    result["venue_review_holds"] = 0\n''',
    "venue counters",
)
server = replace_once(
    server,
    '''    articles = []\n    fetched: dict[str, str] = {}\n''',
    '''    articles = []\n    review_holds = []\n    review_hold_urls: set[str] = set()\n    fetched: dict[str, str] = {}\n\n    def register_review_hold(item: Any) -> None:\n        if item.url not in review_hold_urls:\n            review_holds.append(item)\n            review_hold_urls.add(item.url)\n            result["venue_review_holds"] += 1\n        fetched[item.url] = "primary_source_review_hold"\n''',
    "review hold storage",
)
server = replace_once(
    server,
    '''        collectors.update({\n            key: build_collector(source, client) for key, source in venues.items()\n        })\n''',
    '''        collectors.update({\n            key: build_collector(source, client) for key, source in runtime_venues.items()\n        })\n''',
    "runtime venue collectors",
)
server = replace_once(
    server,
    '''            venue_reason = (event_content_reason(item.title, item.body, now=now)\n                            if source_id in venues else None)\n            if fresh.action != "keep" or venue_reason is not None:\n                result["discarded_before_persistence"] += 1\n                fetched[url] = venue_reason or fresh.reason\n                return fetched[url]\n            articles.append(item)\n''',
    '''            venue_reason = (event_content_reason(item.title, item.body, now=now)\n                            if source_id in runtime_venues else None)\n            if (source_id in review_venues and venue_reason is None\n                    and _is_review_only_venue_hold(item, now=now)):\n                register_review_hold(item)\n                return fetched[url]\n            if fresh.action != "keep" or venue_reason is not None:\n                result["discarded_before_persistence"] += 1\n                fetched[url] = venue_reason or fresh.reason\n                return fetched[url]\n            articles.append(item)\n''',
    "direct review hold",
)
server = replace_once(
    server,
    '''                if await resolve_link(url) == "primary_source_collected":\n                    linked.append(url)\n''',
    '''                if await resolve_link(url) in {\n                    "primary_source_collected", "primary_source_review_hold"\n                }:\n                    linked.append(url)\n''',
    "submission review hold link",
)
server = replace_once(
    server,
    '''        for source_id in (() if submissions_only else venues):\n            collector = collectors[source_id]\n            try:\n                for item in await collector.collect():\n                    result["venue_items"] += 1\n                    reason = event_content_reason(item.title, item.body, now=now)\n                    fresh = freshness_decision(item.title, item.body, item.published_at, now)\n                    if reason is not None or fresh.action != "keep":\n                        result["venue_deferred"] += 1\n                        continue\n                    articles.append(item)\n                    fetched[item.url] = "primary_source_collected"\n''',
    '''        for source_id in (() if submissions_only else runtime_venues):\n            collector = collectors[source_id]\n            try:\n                for item in await collector.collect():\n                    result["venue_items"] += 1\n                    reason = event_content_reason(item.title, item.body, now=now)\n                    fresh = freshness_decision(item.title, item.body, item.published_at, now)\n                    if (source_id in review_venues and reason is None\n                            and _is_review_only_venue_hold(item, now=now)):\n                        result["venue_deferred"] += 1\n                        register_review_hold(item)\n                        continue\n                    if reason is not None or fresh.action != "keep":\n                        result["venue_deferred"] += 1\n                        continue\n                    articles.append(item)\n                    fetched[item.url] = "primary_source_collected"\n''',
    "review-only venue loop",
)
server = replace_once(
    server,
    '''        written, changes = capture_collected_batch(session, articles)\n''',
    '''        written, changes = capture_collected_batch(session, [*articles, *review_holds])\n''',
    "persist review holds",
)
SERVER.write_text(server, encoding="utf-8")

tests = TESTS.read_text(encoding="utf-8")
tests = replace_once(
    tests,
    '''    assert "venue_bitefartcafe" not in venues\n    assert "venue_yugoslavia" not in venues\n\n    discovery = server_cycle.discovery_venue_sources()\n''',
    '''    assert "venue_bitefartcafe" not in venues\n    assert "venue_yugoslavia" not in venues\n\n    review_only = server_cycle.review_only_venue_sources()\n    assert set(review_only) == {"venue_bitefartcafe", "venue_yugoslavia"}\n    assert all(not source.policy.auto_publish for source in review_only.values())\n\n    discovery = server_cycle.discovery_venue_sources()\n''',
    "review-only source regression",
)
if "test_review_only_venue_hold_requires_future_event_and_unknown_publication" in tests:
    raise RuntimeError("review-only hold regression already exists")
tests += '''\n\n\ndef test_review_only_venue_hold_requires_future_event_and_unknown_publication():\n    now = datetime(2026, 10, 22, 12, tzinfo=UTC)\n    future = CollectedItem(\n        source_id="venue_bitefartcafe",\n        external_id="https://bitefartcafe.rs/dogadjaji/example/",\n        url="https://bitefartcafe.rs/dogadjaji/example/",\n        title="Koncert 30.10.2026.",\n        body="Koncert će se održati 30.10.2026. u Beogradu. Ulaznice su dostupne.",\n        published_at=None,\n    )\n    assert server_cycle._is_review_only_venue_hold(future, now=now)\n\n    expired = future.__class__(\n        source_id=future.source_id,\n        external_id=future.external_id + "expired",\n        url=future.url + "expired",\n        title="Koncert 20.10.2026.",\n        body="Koncert je održan 20.10.2026. u Beogradu. Ulaznice su bile dostupne.",\n        published_at=None,\n    )\n    assert not server_cycle._is_review_only_venue_hold(expired, now=now)\n\n    dated = future.__class__(\n        source_id=future.source_id,\n        external_id=future.external_id + "dated",\n        url=future.url + "dated",\n        title=future.title,\n        body=future.body,\n        published_at=now - timedelta(hours=1),\n    )\n    assert not server_cycle._is_review_only_venue_hold(dated, now=now)\n\n\ndef test_unknown_publication_date_triages_to_human_review_without_ai(seeded):\n    from relocant_media.ops.newsroom_triage import triage_queue\n\n    engine, raw_id, _ = seeded\n    with Session(engine) as session:\n        item = session.get(RawItem, raw_id)\n        item.published_at = None\n        session.commit()\n\n    report = triage_queue(engine, now=datetime.now(UTC))\n    assert report["metadata_deferred"] == 1\n    assert report["ai_calls"] == 0\n    with Session(engine) as session:\n        run = session.get(EditorialRun, raw_id)\n        assert run is not None\n        assert run.outcome["status"] == "human_review"\n        assert run.outcome["reason"] == "publication_date_unknown"\n        assert run.outcome["ai_calls"] == 0\n        assert run.post_id is None\n'''
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
    ["git", "commit", "-m", "Persist undated venue evidence for review"],
    cwd=ROOT,
    check=True,
)
subprocess.run(
    ["git", "push", "origin", f"HEAD:refs/heads/{BRANCH}"],
    cwd=ROOT,
    check=True,
)
