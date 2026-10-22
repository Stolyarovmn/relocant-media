from pathlib import Path

from relocant_media.sources.registry import load_source_registry


def test_tob_events_is_structured_review_only_source() -> None:
    sources = load_source_registry(Path("cities/belgrade/sources"))
    source = next(item for item in sources if item.id == "tob_events")

    assert source.ownership == "official"
    assert source.trust == 0.95
    assert source.extraction.strategy == "tob_events"
    assert source.extraction.status == "active"
    assert source.extraction.approved_hosts == ("tob.rs",)
    assert source.policy.high_risk is False
    assert source.policy.auto_publish is False
    assert source.verification.checked_at is not None
    assert str(source.url) == "https://www.tob.rs/sr/events"
