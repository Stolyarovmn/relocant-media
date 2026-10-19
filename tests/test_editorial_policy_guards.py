from relocant_media.services.editorial import _auto_publish_policy_reason
from relocant_media.storage.models import RawItem, Source


def source(
    source_id: str,
    *,
    ownership: str = "official",
    auto_publish: bool = True,
    high_risk: bool = False,
) -> Source:
    return Source(
        id=source_id,
        city="belgrade",
        name=source_id,
        url=f"https://{source_id}.example",
        source_type="test",
        trust=1,
        ownership=ownership,
        auto_publish=auto_publish,
        high_risk=high_risk,
        enabled=True,
    )


def item(source_id: str, external_id: str = "1") -> RawItem:
    return RawItem(
        source_id=source_id,
        external_id=external_id,
        url=f"https://{source_id}.example/{external_id}",
        title="Заголовок",
        body="Текст",
    )


def test_official_auto_publish_source_passes() -> None:
    official = source("city")
    assert _auto_publish_policy_reason(
        [(item("city"), official)], anchor_source=official
    ) is None


def test_nonofficial_event_requires_official_primary_source() -> None:
    media = source("media", ownership="independent_media")
    assert _auto_publish_policy_reason(
        [(item("media"), media)], anchor_source=media
    ) == "official_source_required"


def test_source_can_disable_auto_publish_even_with_official_evidence() -> None:
    official = source("city", auto_publish=False)
    assert _auto_publish_policy_reason(
        [(item("city"), official)], anchor_source=official
    ) == "source_auto_publish_disabled"


def test_any_high_risk_evidence_forces_review() -> None:
    official = source("city")
    risky = source("media", ownership="media", high_risk=True)
    assert _auto_publish_policy_reason(
        [(item("city"), official), (item("media", "2"), risky)],
        anchor_source=official,
    ) == "source_policy_requires_review"
