"""Deterministic first-party fallback card bound to an already reviewed package."""

from __future__ import annotations

import binascii
import hashlib
import json
import os
import struct
import zlib
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from relocant_media.ai.package import EditorialPackage, canonical_json
from relocant_media.bot.media import MediaAsset
from relocant_media.storage.models import EditorialRun, Post

CARD_WIDTH = 1200
CARD_HEIGHT = 630
CARD_FILE_NAME = "self-authored-card.png"

# Deliberately generic visual classes: the card never depicts an event, person or place.
# The approved category changes only the abstract symbol/palette.
_PALETTES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "transport": ((24, 43, 77), (86, 168, 235)),
    "events": ((61, 34, 86), (212, 125, 222)),
    "health": ((42, 69, 54), (104, 190, 129)),
    "documents": ((64, 52, 36), (235, 177, 83)),
    "services": ((36, 57, 70), (87, 181, 195)),
    "city": ((45, 49, 66), (137, 145, 185)),
}


def _category(value: str) -> str:
    normalized = value.strip().casefold()
    for key in ("transport", "events", "health", "documents", "services"):
        if key in normalized:
            return key
    return "city"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", binascii.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _set_pixel(canvas: bytearray, x: int, y: int, rgb: tuple[int, int, int]) -> None:
    if not 0 <= x < CARD_WIDTH or not 0 <= y < CARD_HEIGHT:
        return
    offset = (y * CARD_WIDTH + x) * 3
    canvas[offset : offset + 3] = bytes(rgb)


def _rect(
    canvas: bytearray,
    left: int,
    top: int,
    right: int,
    bottom: int,
    rgb: tuple[int, int, int],
) -> None:
    left, right = max(left, 0), min(right, CARD_WIDTH)
    top, bottom = max(top, 0), min(bottom, CARD_HEIGHT)
    if left >= right or top >= bottom:
        return
    row = bytes(rgb) * (right - left)
    for y in range(top, bottom):
        offset = (y * CARD_WIDTH + left) * 3
        canvas[offset : offset + len(row)] = row


def _circle(
    canvas: bytearray,
    center_x: int,
    center_y: int,
    radius: int,
    rgb: tuple[int, int, int],
) -> None:
    radius_squared = radius * radius
    for y in range(max(0, center_y - radius), min(CARD_HEIGHT, center_y + radius + 1)):
        dy = y - center_y
        span = int((radius_squared - dy * dy) ** 0.5)
        _rect(canvas, center_x - span, y, center_x + span + 1, y + 1, rgb)


def _draw_symbol(canvas: bytearray, category: str, accent: tuple[int, int, int]) -> None:
    white = (245, 247, 250)
    if category == "transport":
        _rect(canvas, 430, 195, 770, 410, white)
        _rect(canvas, 470, 235, 575, 315, accent)
        _rect(canvas, 625, 235, 730, 315, accent)
        _circle(canvas, 505, 430, 38, white)
        _circle(canvas, 695, 430, 38, white)
    elif category == "events":
        _rect(canvas, 430, 190, 770, 450, white)
        _rect(canvas, 430, 250, 770, 450, accent)
        _rect(canvas, 485, 160, 525, 235, white)
        _rect(canvas, 675, 160, 715, 235, white)
        _circle(canvas, 600, 350, 58, white)
    elif category == "health":
        _rect(canvas, 545, 170, 655, 460, white)
        _rect(canvas, 455, 260, 745, 370, white)
    elif category == "documents":
        _rect(canvas, 455, 160, 745, 470, white)
        _rect(canvas, 510, 245, 690, 275, accent)
        _rect(canvas, 510, 315, 690, 345, accent)
        _rect(canvas, 510, 385, 640, 415, accent)
    elif category == "services":
        _circle(canvas, 600, 315, 155, white)
        _circle(canvas, 600, 315, 82, accent)
        _rect(canvas, 570, 115, 630, 205, white)
        _rect(canvas, 570, 425, 630, 515, white)
        _rect(canvas, 400, 285, 490, 345, white)
        _rect(canvas, 710, 285, 800, 345, white)
    else:
        _rect(canvas, 420, 260, 780, 470, white)
        _rect(canvas, 470, 205, 540, 470, white)
        _rect(canvas, 565, 155, 635, 470, white)
        _rect(canvas, 660, 225, 730, 470, white)
        _rect(canvas, 390, 470, 810, 500, white)


def render_card_png(*, category: str, package_sha256: str) -> bytes:
    """Render an abstract first-party PNG; no external image or factual text is used."""
    visual_category = _category(category)
    background, accent = _PALETTES[visual_category]
    canvas = bytearray(bytes(background) * (CARD_WIDTH * CARD_HEIGHT))

    # Bind decoration to the exact reviewed package without displaying new factual claims.
    seed = bytes.fromhex(package_sha256)
    for index in range(12):
        x = 65 + (seed[index] * 4) % 1070
        y = 55 + (seed[index + 12] * 2) % 520
        radius = 12 + seed[index + 20] % 26
        _circle(canvas, x, y, radius, accent)

    _circle(canvas, 600, 315, 205, accent)
    _draw_symbol(canvas, visual_category, accent)
    _rect(canvas, 0, 585, CARD_WIDTH, CARD_HEIGHT, accent)

    raw = b"".join(
        b"\x00" + bytes(canvas[y * CARD_WIDTH * 3 : (y + 1) * CARD_WIDTH * 3])
        for y in range(CARD_HEIGHT)
    )
    header = struct.pack(">IIBBBBB", CARD_WIDTH, CARD_HEIGHT, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(
        b"IDAT", zlib.compress(raw, level=9)
    ) + _chunk(b"IEND", b"")


def _reviewable_https(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.lower() == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
    )


def create_self_authored_card_bundle(
    session: Session,
    raw_item_id: int,
    output: Path,
) -> dict[str, object]:
    """Create review input for a pending reviewed post; never create reviews or publish."""
    if os.environ.get("APP_ENV", "").lower() != "production":
        raise ValueError("APP_ENV=production required")
    if output.exists():
        raise ValueError("new output directory required")

    run = session.get(EditorialRun, raw_item_id)
    if run is None or run.post_id is None or run.outcome.get("status") != "ready":
        raise ValueError("ready reviewed editorial run required")
    if run.delivery_status != "pending":
        raise ValueError("self-authored card can only be prepared before delivery")
    post = session.get(Post, run.post_id)
    if post is None or post.telegram_message_id is not None:
        raise ValueError("pending undelivered post required")

    package_value = run.outcome.get("_package")
    package_sha256 = run.outcome.get("_package_sha256")
    if not isinstance(package_value, dict) or not isinstance(package_sha256, str):
        raise ValueError("reviewed editorial package required")
    package = EditorialPackage.model_validate_json(json.dumps(package_value))
    actual_sha256 = hashlib.sha256(
        canonical_json(package.model_dump(mode="json")).encode()
    ).hexdigest()
    if package.raw_item_id != raw_item_id or actual_sha256 != package_sha256:
        raise ValueError("editorial package identity or fingerprint mismatch")
    if not _reviewable_https(package.evidence.url):
        raise ValueError("reviewable HTTPS primary source required")

    assets = output / "assets"
    assets.mkdir(parents=True, exist_ok=False)
    png = render_card_png(
        category=package.outcome.ranking.category,
        package_sha256=package_sha256,
    )
    card = assets / CARD_FILE_NAME
    card.write_bytes(png)
    digest = hashlib.sha256(png).hexdigest()
    media_asset = MediaAsset(
        file_name=CARD_FILE_NAME,
        sha256=digest,
        origin="self_authored_card",
        source_url=package.evidence.url,
        rights_basis="Self-authored deterministic graphic with no third-party image bytes",
        rights_verified=True,
        attribution="@eduvbelgrad",
    )
    (output / "assets.json").write_text(
        json.dumps({"assets": [media_asset.model_dump(mode="json")]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    generation = {
        "version": 1,
        "raw_item_id": raw_item_id,
        "post_id": run.post_id,
        "editorial_package_sha256": package_sha256,
        "source_url": package.evidence.url,
        "category": package.outcome.ranking.category,
        "asset_sha256": digest,
        "origin": "self_authored_card",
        "third_party_image_bytes_used": False,
        "generated_media_used": True,
        "requires_independent_media_reviews": True,
        "publication_permitted": False,
        "telegram_io_performed": False,
    }
    (output / "generation.json").write_text(
        json.dumps(generation, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return generation
