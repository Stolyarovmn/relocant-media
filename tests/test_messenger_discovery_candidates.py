import json

from relocant_media.collectors.telegram_topics import default_channels_for_city

REGISTRY = "docs/messenger-discovery-candidates.json"


def _load_registry() -> dict[str, object]:
    with open(REGISTRY, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert isinstance(payload, dict)
    return payload


def test_candidate_registry_is_fail_closed() -> None:
    payload = _load_registry()

    assert payload["purpose"] == "messenger_discovery_candidates_only"
    assert payload["policy"] == {
        "runtime_collection": False,
        "publication_evidence": False,
        "requires_current_endpoint_verification_before_promotion": True,
        "requires_primary_verification": True,
    }

    sources = payload["sources"]
    assert isinstance(sources, list)
    assert sources
    assert len({source["id"] for source in sources}) == len(sources)

    for source in sources:
        assert source["role"] == "candidate_only"
        assert source["runtime_collection"] is False
        assert source["publication_evidence"] is False
        assert source["requires_primary_verification"] is True
        assert source["endpoint_status"] != "verified_current"
        assert "url" not in source
        assert source["verification"]["official_evidence_urls"]


def test_novi_sad_candidate_is_preserved_but_not_enabled() -> None:
    payload = _load_registry()
    sources = payload["sources"]
    candidate = next(source for source in sources if source["id"] == "viber_visit_novi_sad")

    assert candidate["scope"] == ["novi_sad"]
    assert candidate["endpoint_status"] == "unconfirmed_current"
    assert default_channels_for_city("novi_sad") == ()
