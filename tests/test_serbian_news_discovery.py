import json

REGISTRY = "docs/serbian-news-discovery.json"


def _load_registry() -> dict[str, object]:
    with open(REGISTRY, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert isinstance(payload, dict)
    return payload


def _assert_https(value: str) -> None:
    assert value.startswith("https://")
    authority = value.removeprefix("https://").split("/", 1)[0]
    assert authority
    assert "@" not in authority


def test_serbian_news_registry_is_discovery_only_and_fail_closed() -> None:
    payload = _load_registry()

    assert payload["purpose"] == "serbian_news_discovery_only"
    assert payload["policy"] == {
        "publication_evidence": False,
        "requires_primary_verification": True,
        "multiple_media_reports_are_not_independent_primary_confirmation": True,
        "follow_claim_to_primary_source_when_available": True,
    }

    sources = payload["sources"]
    assert isinstance(sources, list)
    assert sources
    assert len({source["id"] for source in sources}) == len(sources)

    for source in sources:
        assert isinstance(source, dict)
        assert source["role"] == "discovery_only"
        assert source["publication_evidence"] is False
        assert source["requires_primary_verification"] is True
        assert source["authoritative_for"] == []
        _assert_https(source["url"])
        if local_url := source.get("local_url"):
            assert isinstance(local_url, str)
            _assert_https(local_url)
        feeds = source["feeds"]
        assert isinstance(feeds, list)
        for feed in feeds:
            assert isinstance(feed, dict)
            _assert_https(feed["url"])
            assert feed["status"] in {
                "verified_current",
                "advertised_by_official_rss_index",
            }
            assert feed["verified_on"] == payload["observed_on"]


def test_unverified_media_rss_endpoints_are_not_invented() -> None:
    payload = _load_registry()
    sources = payload["sources"]
    assert isinstance(sources, list)
    by_id = {source["id"]: source for source in sources if isinstance(source, dict)}

    for source_id in {"n1_serbia", "danas_news", "nova_news", "vreme_news", "birn_serbia"}:
        assert by_id[source_id]["feeds"] == []
