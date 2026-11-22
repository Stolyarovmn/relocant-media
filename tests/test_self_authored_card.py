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


POST_149_URL = ("https://www.beograd.rs/lat/beoinfo-vesti/a116747/"
                "Radovi-na-vodovodnoj-mrezi-u-utorak.html")


def _post_149(second_day: str = "utorak, 24.", **changes):
    """Объявление из поста №149 в том виде, в каком его хранит сборщик городского сайта."""
    from datetime import UTC, datetime

    from relocant_media.ai.models import EvidenceDocument

    return EvidenceDocument.model_validate({
        "source_id": "city_beograd_news", "url": POST_149_URL,
        "title": "Radovi na vodovodnoj mreži",
        "published_at": datetime(2026, 11, 20, 23, tzinfo=UTC),
        "published_at_precision": "day",
        "body": (
            "Zbog planiranih radova na vodovodnoj mreži u opštini Savski venac, "
            "u utorak, 24. novembra, u periodu od 8 do 15 sati, "
            "bez vode će ostati potrošači u bloku oivičenom Fabrisovom, Zvečanskom, "
            "Dr Milutina Ivkovića i Oblakovskom ulicom.\n"
            f"U opštini Voždovac, u {second_day} novembra, u periodu od 8 do 16 sati, "
            "bez vode će ostati potrošači u ulicama Brane Ćosića (od broja 16 do broja 42), "
            "Katarine Ivanović i Rastka Petrovića."
        ),
        **changes,
    })


def _reviewed_map(document) -> dict:
    """Полный путь: точка карты и расписание map_notice_v2 из того же документа."""
    from relocant_media.services.native_map import _notice_layout
    from relocant_media.services.utility_map import build_utility_point

    point = build_utility_point(document, "")
    assert point is not None
    return {"point": point, **_notice_layout(document, point)}


def _power_notice():
    from datetime import UTC, datetime

    from relocant_media.ai.models import EvidenceDocument

    return EvidenceDocument.model_validate({
        "source_id": "city_beograd_news",
        "url": ("https://www.beograd.rs/lat/servisne-informacije/elektricna-energija/"
                "a116619/Planirana-iskljucenja-na-elektro-mrezi.html"),
        "title": "ELEKTRIČNA ENERGIJA: Planirana isključenja za 13. novembar 2026. godine",
        "published_at": datetime(2026, 11, 12, tzinfo=UTC), "published_at_precision": "day",
        "body": "\n\n".join([
            "Zbog izvođenja planiranih radova bez električne energije:",
            "Vračar 9.00 - 11.00 PRVA ULICA: 42,",
            "Zvezdara 9.00 - 11.00 DRUGA ULICA: 2,",
            "Palilula 9.00 - 11.00 ČETVRTA: 119,",
        ]),
    })


def test_notice_card_for_post_149_takes_kind_and_districts_from_reviewed_parse() -> None:
    from relocant_media.services.native_map import NOTICE_LAYOUT
    from relocant_media.services.notice_card import notice_card_text

    document = _post_149()
    native = _reviewed_map(document)
    assert native["layout"] == NOTICE_LAYOUT
    assert native["point"]["source_id"] == "city_beograd_news"
    assert native["point"]["source_url"] == POST_149_URL
    text = notice_card_text(native, (document,))
    assert text is not None and text.layout == "per_district"
    assert text.lines() == ("Отключение воды", "24.11.2026",
                            "Савски-Венац · 08:00–15:00", "Вождовац · 08:00–16:00")


def test_notice_card_shared_interval_only_when_all_districts_match() -> None:
    from relocant_media.services.notice_card import notice_card_text

    document = _power_notice()
    text = notice_card_text(_reviewed_map(document), (document,))
    assert text is not None and text.layout == "shared_window"
    assert text.lines() == ("Отключение света", "Врачар, Звездара и ещё 1 район",
                            "13.11.2026 · 09:00–11:00")


def test_notice_card_falls_back_to_map_note_across_dates() -> None:
    from relocant_media.services.notice_card import MAP_NOTE, notice_card_text

    document = _post_149("sredu, 25.")
    text = notice_card_text(_reviewed_map(document), (document,))
    assert text is not None and text.layout == "see_map"
    assert text.body == ("24.11.2026 – 25.11.2026", MAP_NOTE)


def test_notice_card_requires_the_reviewed_document_and_matching_source(monkeypatch) -> None:
    from relocant_media.services import notice_card
    from relocant_media.services.notice_card import notice_card_text
    from relocant_media.services.utility_map import utility_document_sha256

    document = _post_149()
    native = _reviewed_map(document)
    assert notice_card_text(native) is None
    assert notice_card_text(native, (_post_149(title="Изменённый текст"),)) is None
    assert notice_card_text({**native, "schedule": native["schedule"][:1]}, (document,)) is None
    assert notice_card_text({**native, "layout": "map_notice_v1"}, (document,)) is None
    assert notice_card_text({**native, "schedule": []}, (document,)) is None
    assert notice_card_text(None, (document,)) is None
    # Профильный источник электросетей не может получить карточку «вода»,
    # даже если разбор по ошибке распознал бы водопровод.
    eds = _post_149(source_id="eds_belgrade_outages_tomorrow")
    point = {**native["point"], "source_id": eds.source_id,
             "document_sha256": utility_document_sha256(eds)}
    monkeypatch.setattr(notice_card, "notice_scope_places",
                        lambda document, point: ("water", [("24.11.2026 · 08:00–15:00",
                                                            "Savski venac")]))
    assert notice_card_text({**native, "point": point}, (document,)) is None
    assert notice_card_text({**native, "point": point}, (eds,)) is None
    monkeypatch.setattr(notice_card, "notice_scope_places",
                        lambda document, point: ("water", [("24.11.2026 · 08:00–15:00",
                                                            "Beograd")]))
    # Неизвестный муниципалитет не выводится на карточку.
    assert notice_card_text(native, (document,)) is None


def test_notice_card_renders_deterministic_png() -> None:
    from relocant_media.services.notice_card import (
        CARD_HEIGHT as NOTICE_HEIGHT,
    )
    from relocant_media.services.notice_card import (
        CARD_WIDTH as NOTICE_WIDTH,
    )
    from relocant_media.services.notice_card import notice_card_text, render_notice_card

    document = _post_149()
    text = notice_card_text(_reviewed_map(document), (document,))
    assert text is not None
    first = render_notice_card(text)
    assert first == render_notice_card(text)
    changed = text.model_copy(update={"body": ("24.11.2026", "Земун · 08:00–15:00")})
    assert first != render_notice_card(changed)
    assert first.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", first[16:24]) == (NOTICE_WIDTH, NOTICE_HEIGHT)
    validate_telegram_photo(Path("notice-card.png"), first)


def test_notice_card_backgrounds_exist_and_long_text_wraps() -> None:
    from relocant_media.services.notice_card import (
        MAP_NOTE,
        NoticeCardText,
        _background,
        render_notice_card,
    )

    for kind in ("water", "power"):
        assert _background(kind).size == (1280, 720)
    cards = (
        NoticeCardText(kind="power", layout="shared_window", headline="Отключение света",
                       body=("Палилула, Нови-Београд и ещё 12 районов",
                             "24.11.2026 · 08:00–15:00")),
        NoticeCardText(kind="power", layout="per_district", headline="Отключение света",
                       body=("24.11.2026", "Нови-Београд · 08:00–15:00",
                             "Савски-Венац · 09:00–16:00", "Звездара · 10:00–17:00")),
        NoticeCardText(kind="water", layout="see_map", headline="Отключение воды",
                       body=("24.11.2026 – 25.11.2026", MAP_NOTE)),
    )
    for card in cards:
        assert render_notice_card(card).startswith(b"\x89PNG\r\n\x1a\n")
