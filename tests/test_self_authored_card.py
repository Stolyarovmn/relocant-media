import hashlib
import json
import struct
from pathlib import Path

import pytest
from test_production_media_delivery import reviewed as _reviewed

from relocant_media.bot.media import MediaAsset
from relocant_media.services.media_release_finalizer import prepare_media_release_review_bundle
from relocant_media.services.production_media import validate_telegram_photo
from relocant_media.services.self_authored_card import (
    CARD_FILE_NAME,
    CARD_HEIGHT,
    CARD_WIDTH,
    create_self_authored_card_bundle,
    render_card_png,
)

reviewed = _reviewed


def test_rendered_card_is_deterministic_valid_size_png() -> None:
    package_sha256 = "ab" * 32

    first = render_card_png(category="transport", package_sha256=package_sha256)
    second = render_card_png(category="transport", package_sha256=package_sha256)
    other = render_card_png(category="events", package_sha256=package_sha256)

    assert first == second
    assert first != other
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = struct.unpack(">II", first[16:24])
    assert (width, height) == (CARD_WIDTH, CARD_HEIGHT)


@pytest.mark.asyncio
async def test_self_authored_card_enters_existing_two_review_pipeline(
    reviewed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    generated = tmp_path / "generated"

    result = create_self_authored_card_bundle(session, run.raw_item_id, generated)

    assert result["origin"] == "self_authored_card"
    assert result["third_party_image_bytes_used"] is False
    assert result["requires_independent_media_reviews"] is True
    assert result["publication_permitted"] is False
    assert result["telegram_io_performed"] is False
    assert result["card_layout"] == "abstract_v1"
    assert result["card_text"] == []
    asset_values = json.loads((generated / "assets.json").read_text(encoding="utf-8"))["assets"]
    asset = MediaAsset.model_validate(asset_values[0])
    assert asset.origin == "self_authored_card"
    assert asset.rights_verified is True
    assert asset.source_url.startswith("https://")
    card = generated / "assets" / CARD_FILE_NAME
    card_bytes = card.read_bytes()
    assert asset.sha256 == hashlib.sha256(card_bytes).hexdigest()
    validate_telegram_photo(card, card_bytes)

    review_bundle = tmp_path / "review" / str(run.raw_item_id)
    prepared = await prepare_media_release_review_bundle(
        session,
        raw_item_id=run.raw_item_id,
        release_id=f"self-card-{run.raw_item_id}",
        assets_manifest=generated / "assets.json",
        assets=generated / "assets",
        output=review_bundle,
    )

    assert prepared["state"] == "awaiting_independent_media_reviews"
    assert prepared["publication_permitted"] is False
    assert prepared["rights_inferred"] is False
    assert prepared["telegram_io_performed"] is False
    assert not (review_bundle / "release.json").exists()


def test_self_authored_card_refuses_non_production(
    reviewed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValueError, match="APP_ENV=production"):
        create_self_authored_card_bundle(session, run.raw_item_id, tmp_path / "generated")


def test_self_authored_card_refuses_after_delivery_claim(
    reviewed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    monkeypatch.setenv("APP_ENV", "production")
    run.delivery_status = "sending"
    session.flush()

    with pytest.raises(ValueError, match="before delivery"):
        create_self_authored_card_bundle(session, run.raw_item_id, tmp_path / "generated")


def _notice_map(source_id: str = "bvk_planned_works", **extra) -> dict:
    return {
        "layout": "map_notice_v2",
        "point": {"mode": "utility_map_review", "source_id": source_id,
                  "source_url": "https://www.bvk.rs/planirani-radovi/1"},
        "schedule": [
            {"when": "24.11.2026 · 08:00–15:00",
             "items": ["Fabrisova, 1 · Савски-Венац", "Zvečanska, 2 · Савски-Венац"],
             "areas": []},
            {"when": "24.11.2026 · 08:00–16:00",
             "items": ["Brane Ćosića, 16 · Вождовац"], "areas": ["Медаковић · Вождовац"]},
        ],
        **extra,
    }


def test_notice_card_text_comes_only_from_reviewed_schedule() -> None:
    from relocant_media.services.native_map import NOTICE_LAYOUT
    from relocant_media.services.notice_card import notice_card_text

    assert NOTICE_LAYOUT == "map_notice_v2"
    text = notice_card_text(_notice_map())
    assert text is not None
    assert text.lines() == ("Отключение воды", "Савски-Венац, Вождовац",
                            "24.11.2026 · 08:00–15:00 и ещё 1 интервал")
    power = notice_card_text(_notice_map("eds_belgrade_outages_tomorrow"))
    assert power is not None and power.headline == "Отключение света"
    many = _notice_map()
    many["schedule"][1]["items"] += ["X, 1 · Земун", "Y, 2 · Врачар"]
    many_text = notice_card_text(many)
    assert many_text is not None and many_text.places == "Савски-Венац, Вождовац и ещё 2 района"
    # Неизвестный вид работ, другой макет или пустое расписание — без карточки.
    city = _notice_map("city_beograd_news")
    assert notice_card_text(city) is None
    city["point"]["source_url"] = ("https://www.beograd.rs/lat/servisne-informacije/"
                                   "vodovod-i-kanalizacija/a1/x.html")
    city_text = notice_card_text(city)
    assert city_text is not None and city_text.kind == "water"
    assert notice_card_text(_notice_map(layout="map_notice_v1")) is None
    assert notice_card_text(_notice_map(schedule=[])) is None
    assert notice_card_text({**_notice_map(), "point": {"mode": "transport_map_review"}}) is None
    assert notice_card_text(None) is None


def test_notice_card_renders_deterministic_png() -> None:
    from relocant_media.services.notice_card import (
        CARD_HEIGHT as NOTICE_HEIGHT,
    )
    from relocant_media.services.notice_card import (
        CARD_WIDTH as NOTICE_WIDTH,
    )
    from relocant_media.services.notice_card import notice_card_text, render_notice_card

    text = notice_card_text(_notice_map())
    assert text is not None
    first = render_notice_card(text)
    assert first == render_notice_card(text)
    assert first != render_notice_card(text.model_copy(update={"places": "Земун"}))
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", first[16:24]) == (NOTICE_WIDTH, NOTICE_HEIGHT)
    validate_telegram_photo(Path("notice-card.png"), first)


def test_notice_card_backgrounds_exist_and_long_text_wraps() -> None:
    from relocant_media.services.notice_card import NoticeCardText, _background, render_notice_card

    for kind in ("water", "power", "heating", "road"):
        assert _background(kind).size == (1280, 720)
    long_text = NoticeCardText(
        kind="power", headline="Отключение света",
        places="Палилула, Нови-Београд и ещё 12 районов",
        when="24.11.2026 · 08:00–15:00 и ещё 14 интервалов",
    )
    assert render_notice_card(long_text).startswith(b"\x89PNG\r\n\x1a\n")
