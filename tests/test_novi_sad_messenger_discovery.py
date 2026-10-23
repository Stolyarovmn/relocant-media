import json

from relocant_media.collectors.telegram_topics import default_channels_for_city
from relocant_media.services.channel_delivery import APPROVED_CHANNELS_BY_CITY

REGISTRY = "cities/novi_sad/messenger-discovery.json"


def test_novi_sad_messenger_catalog_is_stored_but_fail_closed() -> None:
    with open(REGISTRY, encoding="utf-8") as handle:
        payload = json.load(handle)

    assert payload["city"] == "novi_sad"
    assert payload["purpose"] == "city_messenger_discovery_catalog_only"
    assert payload["policy"] == {
        "runtime_collection": False,
        "publication_evidence": False,
        "requires_primary_verification": True,
        "dedicated_channel_required_before_runtime": True,
    }
    assert default_channels_for_city("novi_sad") == ()
    assert "novi_sad" not in APPROVED_CHANNELS_BY_CITY

    sources = payload["sources"]
    assert isinstance(sources, list)
    assert sources
    assert len({source["id"] for source in sources}) == len(sources)

    for source in sources:
        assert source["scope"] == ["novi_sad"]
        assert source["role"] == "discovery_only"
        assert source["collection"]["status"] == "catalog_only"
        assert source["publication_evidence"] is False
        assert source["requires_primary_verification"] is True
        assert source["authoritative_for"] == []
        assert source["verification"]["status"] == "verified_current"
        assert source["verification"]["verified_on"] == payload["observed_on"]


def test_visit_novi_sad_viber_is_preserved_for_future_city_channel() -> None:
    with open(REGISTRY, encoding="utf-8") as handle:
        payload = json.load(handle)

    source = next(source for source in payload["sources"] if source["id"] == "viber_visit_novi_sad")
    assert source["platform"] == "viber"
    assert source["name"] == "Visit Novi Sad"
    assert source["url"].startswith("https://invite.viber.com/")
