import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session
from test_editorial_integration import prepare_service
from test_editorial_package import bind_context, package_payload, parse

from relocant_media.ai.package import canonical_json
from relocant_media.bot.api import PhotoUpload
from relocant_media.bot.media import MediaRelease, media_context_sha256, validate_media
from relocant_media.services.media_delivery import deliver_media_release
from relocant_media.services.package_import import import_package

editorial_session = _editorial_session


def release_for(tmp_path: Path, raw_id: int, post_id: int, package: dict) -> MediaRelease:
    caption = "Точный русский текст"
    hashes = []
    assets = []
    for i in range(2):
        path = tmp_path / f"card-{i}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([i]))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append(digest)
        assets.append({"file_name": path.name, "sha256": digest,
                       "origin": "self_authored_card", "source_url": "https://example.org/",
                       "rights_basis": "Собственная информационная графика",
                       "rights_verified": True,
                       "attribution": "Редакция"})
    release = MediaRelease.model_validate({
        "version": 1, "release_id": "test-media-v1", "raw_item_id": raw_id, "post_id": post_id,
        "editorial_package_sha256": hashlib.sha256(canonical_json(package).encode()).hexdigest(),
        "target_id": "-10012345", "caption": caption, "assets": assets,
        "reviews": [{"reviewer_id": role, "role": role, "verdict": "approve", "score": 90,
                     "flags": [], "live_source_checked": True, "checked_at": datetime.now(UTC),
                     "reviewed_asset_sha256": hashes,
                     "caption_sha256": hashlib.sha256(caption.encode()).hexdigest(),
                     "context_sha256": "0" * 64,
                     "rationale": "Проверено независимо"} for role in ("facts", "utility")],
    })
    return bind_media_context(release)


def bind_media_context(release: MediaRelease) -> MediaRelease:
    return release.model_copy(update={"reviews": tuple(
        r.model_copy(update={"context_sha256": media_context_sha256(release)})
        for r in release.reviews
    )})


def rebind_caption(release: MediaRelease, caption: str) -> MediaRelease:
    return bind_media_context(release.model_copy(update={"caption": caption, "reviews": tuple(
        r.model_copy(update={"caption_sha256": hashlib.sha256(caption.encode()).hexdigest()})
        for r in release.reviews
    )}))


def test_media_blocks_changed_asset_caption_and_expired_reviews(tmp_path: Path) -> None:
    release = release_for(tmp_path, 1, 1, {})
    assert len(validate_media(release, tmp_path)) == 2
    with pytest.raises(ValueError, match="mismatched"):
        validate_media(release.model_copy(update={"caption": "Изменено"}), tmp_path)
    altered = release.assets[0].model_copy(
        update={"rights_basis": "Другая версия прав использования"},
    )
    with pytest.raises(ValueError, match="mismatched"):
        validate_media(
            release.model_copy(update={"assets": (altered, release.assets[1])}), tmp_path,
        )
    with pytest.raises(ValueError, match="expired"):
        validate_media(release, tmp_path, now=datetime.now(UTC) + timedelta(hours=25))
    (tmp_path / "card-0.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed after review"):
        validate_media(release, tmp_path)


def test_photo_requires_verified_rights_year_credit_and_archive_label(tmp_path: Path) -> None:
    release = release_for(tmp_path, 1, 1, {})
    asset = release.assets[0].model_copy(update={"rights_verified": False})
    with pytest.raises(ValueError, match="rights"):
        validate_media(release.model_copy(update={"assets": (asset, release.assets[1])}), tmp_path)
    asset = release.assets[0].model_copy(update={"origin": "licensed_photo"})
    with pytest.raises(ValueError, match="year must be known"):
        validate_media(release.model_copy(update={"assets": (asset, release.assets[1])}), tmp_path)
    asset = asset.model_copy(update={"archive_year": 2025, "archive_label": "Фото 2025 года"})
    with pytest.raises(ValueError, match="label"):
        validate_media(release.model_copy(update={"assets": (asset, release.assets[1])}), tmp_path)
    caption = release.caption + "\nФото 2025 года. Редакция"
    valid = rebind_caption(
        release.model_copy(update={"assets": (asset, release.assets[1])}), caption,
    )
    assert len(validate_media(valid, tmp_path)) == 2


class FakePublisher:
    target_id = "-10012345"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def publish(self, caption: str, photos: tuple[PhotoUpload, ...]) -> list[int]:
        self.calls += 1
        if self.fail:
            raise TimeoutError("unknown outcome")
        return [50 + i for i in range(len(photos))]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_reviewed_media_delivery_durable_at_most_once(
    editorial_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail: bool,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    service, provider, raw_id = prepare_service(editorial_session)
    payload = package_payload()
    payload["raw_item_id"] = raw_id
    for review in payload["reviews"]:
        review["reviewed_at"] = datetime.now(UTC).isoformat()
    bind_context(payload)
    package = parse(payload)
    run = import_package(editorial_session, package)
    release = release_for(tmp_path, raw_id, run.post_id, package.model_dump(mode="json"))
    preview = await service.deliver(raw_id, dry_run=True)
    release = rebind_caption(release, preview.text)
    publisher = FakePublisher(fail=fail)
    kwargs = {"assets": tmp_path, "receipts": tmp_path / "receipts", "publisher": publisher,
              "production_id": "-100999", "dry_run": False}
    dry = await deliver_media_release(editorial_session, release, **{**kwargs, "dry_run": True})
    assert dry["state"] == "dry_run" and publisher.calls == 0
    if fail:
        with pytest.raises(TimeoutError):
            await deliver_media_release(editorial_session, release, **kwargs)
        with pytest.raises(ValueError, match="uncertain"):
            await deliver_media_release(editorial_session, release, **kwargs)
    else:
        first = await deliver_media_release(editorial_session, release, **kwargs)
        assert first["state"] == "sent" and first["message_ids"] == [50, 51]
        assert await deliver_media_release(editorial_session, release, **kwargs) == first
        changed = release.model_copy(update={"reviews": tuple(
            r.model_copy(update={"rationale": "Другое ревью"}) for r in release.reviews
        )})
        with pytest.raises(ValueError, match="different content"):
            await deliver_media_release(editorial_session, changed, **kwargs)
    assert publisher.calls == 1 and provider.calls == []
    assert run.delivery_status == "pending" and run.staging_message_id is None
