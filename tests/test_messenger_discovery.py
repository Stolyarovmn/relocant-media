import json

from relocant_media.collectors.telegram_topics import (
    DEFAULT_CHANNELS,
    default_channels_for_city,
)

REGISTRY = "docs/messenger-discovery.json"


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


def test_messenger_registry_is_discovery_only_and_fail_closed() -> None:
    payload = _load_registry()

    assert payload["purpose"] == "messenger_discovery_only"
    assert payload["policy"] == {
        "publication_evidence": False,
        "requires_primary_verification": True,
        "messenger_messages_are_not_independent_primary_confirmation": True,
        "ugc_requires_independent_primary_confirmation": True,
        "runtime_collection_requires_read_only_public_adapter": True,
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
        assert source["platform"] in {"telegram", "viber", "whatsapp"}
        assert source["scope"]
        _assert_https(source["url"])

        collection = source["collection"]
        assert collection["status"] in {"runtime_public_preview", "catalog_only"}
        if collection["status"] == "runtime_public_preview":
            assert source["platform"] == "telegram"
            assert collection["adapter"] == "telegram_public_preview"
            assert collection["account_required"] is False
        else:
            assert collection["reason"]

        verification = source["verification"]
        assert verification["status"] == "verified_current"
        assert verification["verified_on"] == payload["observed_on"]
        assert verification["verification_urls"]
        for url in verification["verification_urls"]:
            _assert_https(url)


def test_only_explicitly_supported_messenger_sources_are_runtime_collected() -> None:
    payload = _load_registry()
    sources = payload["sources"]
    assert isinstance(sources, list)
    runtime = {
        source["id"]: source
        for source in sources
        if isinstance(source, dict)
        and source["collection"]["status"] == "runtime_public_preview"
    }

    assert set(runtime) == {
        "telegram_serbialive_beograd",
        "telegram_moj_beograd",
    }
    assert runtime["telegram_serbialive_beograd"]["url"] == (
        "https://t.me/s/serbialive_beograd"
    )
    assert runtime["telegram_moj_beograd"]["url"] == (
        "https://t.me/s/ylcutcTMU01kNWVk"
    )
    assert all(source["scope"] == ["belgrade"] for source in runtime.values())
    assert "serbialive_beograd" in default_channels_for_city("belgrade")
    assert "ylcutcTMU01kNWVk" in default_channels_for_city("belgrade")
    assert default_channels_for_city("novi_sad") == ()


def test_catalog_only_sources_are_not_silently_enabled() -> None:
    payload = _load_registry()
    sources = payload["sources"]
    assert isinstance(sources, list)

    catalog_only = {
        source["id"]
        for source in sources
        if isinstance(source, dict) and source["collection"]["status"] == "catalog_only"
    }
    assert {
        "telegram_rabotavserbii",
        "telegram_serbiawork",
        "viber_air_serbia",
        "viber_blic",
        "viber_n1_srbija",
        "whatsapp_blic",
        "whatsapp_n1_srbija",
        "whatsapp_nova_s",
        "viber_beograd_uzivo",
    } <= catalog_only
    assert "rabotavserbii" not in DEFAULT_CHANNELS
    assert "serbiawork" not in DEFAULT_CHANNELS
