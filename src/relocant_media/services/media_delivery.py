"""Separate durable staging receipt for an explicitly authorized illustrated release."""

import hashlib
import json
import os
from pathlib import Path
from typing import Protocol, cast

from sqlalchemy.orm import Session

from relocant_media.ai.package import canonical_json
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.api import PhotoUpload
from relocant_media.bot.media import MediaRelease, validate_media
from relocant_media.services.editorial import EditorialService
from relocant_media.storage.models import EditorialRun


class MediaPublisher(Protocol):
    async def publish(self, caption: str, photos: tuple[PhotoUpload, ...]) -> list[int]: ...


def _write_receipt(path: Path, value: dict[str, object], *, exclusive: bool = False) -> None:
    target = path if exclusive else path.with_suffix(".tmp")
    with target.open("x" if exclusive else "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    if not exclusive:
        os.replace(target, path)


async def deliver_media_release(
    session: Session, release: MediaRelease, *, assets: Path, receipts: Path,
    publisher: MediaPublisher, production_id: str, dry_run: bool = True,
) -> dict[str, object]:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("APP_ENV=staging required")
    if not production_id or production_id == release.target_id:
        raise ValueError("known production exclusion required")
    if getattr(publisher, "target_id", None) != release.target_id:
        raise ValueError("publisher target does not match reviewed release")
    paths = validate_media(release, assets)
    uploads = []
    for path, asset in zip(paths, release.assets, strict=True):
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != asset.sha256:
            raise ValueError("media changed while preparing upload")
        uploads.append(PhotoUpload(path.name, data))
    run = session.get(EditorialRun, release.raw_item_id)
    if run is None or run.post_id != release.post_id or "_package" not in run.outcome:
        raise ValueError("reviewed editorial package required")
    fingerprint = hashlib.sha256(canonical_json(run.outcome["_package"]).encode()).hexdigest()
    if fingerprint != release.editorial_package_sha256:
        raise ValueError("editorial package changed")
    # No prepare/generation is used: normal policy, evidence and expiry checks still apply.
    service = EditorialService(session, cast(EditorialPipeline, None))
    preview = await service.deliver(release.raw_item_id, dry_run=True)
    if release.caption != preview.text:
        raise ValueError("media caption must equal the independently reviewed editorial text")
    digest = hashlib.sha256(canonical_json(release.model_dump(mode="json")).encode()).hexdigest()
    if dry_run:
        return {"state": "dry_run", "release_sha256": digest, "image_count": len(paths)}
    receipts.mkdir(parents=True, exist_ok=True)
    path = receipts / f"{release.release_id}.json"
    state: dict[str, object] = {
        "state": "sending", "release_sha256": digest, "target_id": release.target_id,
        "raw_item_id": release.raw_item_id, "post_id": release.post_id,
    }
    try:
        _write_receipt(path, state, exclusive=True)
    except FileExistsError:
        existing: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("release_sha256") != digest:
            raise ValueError("release id already bound to different content") from None
        if existing.get("state") == "sent":
            return existing
        raise ValueError("delivery uncertain; reconcile before another attempt") from None
    try:
        ids = await publisher.publish(release.caption, tuple(uploads))
        if (len(ids) != len(paths) or len(set(ids)) != len(ids)
                or any(type(value) is not int or value <= 0 for value in ids)):
            raise ValueError("invalid media delivery receipt")
        result = {**state, "state": "sent", "message_ids": ids}
        _write_receipt(path, result)
        return result
    except BaseException:
        _write_receipt(path, {**state, "state": "uncertain"})
        raise
