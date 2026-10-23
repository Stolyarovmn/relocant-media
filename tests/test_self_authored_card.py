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
