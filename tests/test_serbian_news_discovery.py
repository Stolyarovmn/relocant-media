import json
from pathlib import Path
from urllib.parse import urlparse


REGISTRY = Path("docs/serbian-news-discovery.json")


def _assert_https(value: str) -> None:
    parsed = urlparse(value)
    assert parsed.scheme == "https"
    assert parsed.netloc
    assert parsed.username is None
    assert parsed.password is None


def test_serbian_news_registry_is_discovery_only_and_fail_closed() -> None:
    payload = json.loads(REGISTRY.read_text(encoding="utf-8"))

    assert payload["purpose"] == "serbian_news_discovery_only"
    assert payload["policy"] == {
        "publication_evidence": False,
        "requires_primary_verification": True,
        "multiple_media_reports_are_not_independent_primary_confirmation": True,
        "follow_claim_to_primary_source_when_available": True,
    }

    sources = payload["sources"]
    assert sources
    assert len({source["id"] for source in sources}) == len(sources)

    for source in sources:
        assert source["role"] == "discovery_only"
        assert source["publication_evidence"] is False
        assert source["requires_primary_verification"] is True
        assert source["authoritative_for"] == []
        _assert_https(source["url"])
        if local_url := source.get("local_url"):
            _assert_https(local_url)
        for feed in source["feeds"]:
            _assert_https(feed["url"])
            assert feed["status"] in {
                "verified_current",
                "advertised_by_official_rss_index",
            }
            assert feed["verified_on"] == payload["observed_on"]


def test_unverified_media_rss_endpoints_are_not_invented() -> None:
    payload = json.loads(REGISTRY.read_text(encoding="utf-8"))
    by_id = {source["id"]: source for source in payload["sources"]}

    for source_id in {"n1_serbia", "danas_news", "nova_news", "vreme_news", "birn_serbia"}:
        assert by_id[source_id]["feeds"] == []
