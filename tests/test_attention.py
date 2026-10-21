from types import SimpleNamespace

from relocant_media.domain.attention import source_priorities, venue_attention
from relocant_media.domain.editorial_heuristics import boilerplate_reason


def lead(text, channel="local", url="https://t.me/local/1"):
    return SimpleNamespace(text=text, channel=channel, url=url)


def test_unknown_is_not_zero_attendance_or_approval():
    result = venue_attention([], {"museum": ("Музей Югославии",)})["museum"]
    assert result["status"] == "unknown" and result["attendance"] is None
    assert not result["publication_permission"]


def test_mentions_are_deduplicated_and_channels_are_independent():
    leads = [lead("BitefArtCafe"), lead("BitefArtCafe"),
             lead("Bitef Art Cafe", "second", "https://t.me/second/2")]
    result = venue_attention(leads, {"venue": ("BitefArtCafe", "Bitef Art Cafe")})
    assert result["venue"]["post_count"] == 2
    assert result["venue"]["distinct_channels"] == 2
    assert source_priorities(result)["venue"] == 2


def test_alias_boundaries_prevent_partial_brand_matching():
    result = venue_attention([lead("MusicologyOther")], {"venue": ("Musicology",)})
    assert result["venue"]["status"] == "unknown"


def test_ceremonial_protocol_has_no_ai_value_without_change():
    assert boilerplate_reason("Потписан Интерфејс протокол за метро", "Одржана свечаност")
    assert boilerplate_reason("Подписан протокол", "Перекрытие дороги начнётся завтра") is None
    assert boilerplate_reason("Отмена рейсов", "Завтра не будет автобусов") is None
