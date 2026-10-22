import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from test_editorial_integration import prepare_service
from test_editorial_package import bind_context, package_payload, parse

from relocant_media.ai.package import EditorialPackage, canonical_json, draft_sha256
from relocant_media.ai.pipeline import EditorialPipeline
from relocant_media.bot.media import MediaRelease, media_context_sha256
from relocant_media.domain.dedup import event_key
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID, deliver_reviewed
from relocant_media.services.editorial import (
    DeliveryBlockedError,
    EditorialService,
    _candidate,
    _snapshot_member,
)
from relocant_media.services.package_import import import_package
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source
from relocant_media.storage.sqlite_schema import initialize_sqlite


@pytest.fixture
def reviewed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EDITORIAL_CHANNEL_MODE", "public")
    database = tmp_path / "editorial.sqlite3"
    initialize_sqlite(database)
    engine = create_engine(f"sqlite:///{database}")
    with Session(engine) as session:
        _, _, raw_id = prepare_service(session)
        item = session.get(RawItem, raw_id)
        assert item is not None
        source = session.get(Source, item.source_id)
        assert source is not None
        payload = package_payload()
        payload.update(version=2, policy_version="subscription-editorial-v2", raw_item_id=raw_id)
        payload["event"] = {
            "event_key": event_key(_candidate(item)),
            "editorial_rule_version": "belgrade-editorial-v2",
            "documents": [_snapshot_member(item, source)],
        }
        draft = parse({**payload, "reviews": payload["reviews"]}).outcome.draft
        for review in payload["reviews"]:
            review["reviewed_at"] = datetime.now(UTC).isoformat()
            review["draft_sha256"] = draft_sha256(draft)
        bind_context(payload)
        run = import_package(session, parse(payload))
        yield session, run
    engine.dispose()


class FakeMediaBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[str, int, str]] = []
        self.fail = fail

    async def send_photo(self, chat_id, photo, caption="", caption_entities=()):
        self.calls.append(("photo", chat_id, caption))
        if self.fail:
            raise TimeoutError("unknown media outcome")
        return 701

    async def send_media_group(self, chat_id, photos, caption="", caption_entities=()):
        self.calls.append(("album", chat_id, caption))
        if self.fail:
            raise TimeoutError("unknown media outcome")
        return [701 + index for index in range(len(photos))]


class ProductionPublisher:
    def __init__(self, *, fail_media: bool = False) -> None:
        self.bot = FakeMediaBot(fail=fail_media)
        self.text_calls: list[str] = []

    async def publish(self, text: str) -> int:
        self.text_calls.append(text)
        return 601


async def _preview(session: Session, raw_id: int) -> str:
    service = EditorialService(session, cast(EditorialPipeline, None))
    return (await service.deliver(raw_id, dry_run=True)).text


def _stored_package(run: EditorialRun) -> EditorialPackage:
    return EditorialPackage.model_validate_json(json.dumps(run.outcome["_package"]))


def _write_release(
    root: Path,
    run: EditorialRun,
    package: EditorialPackage,
    caption: str,
    *,
    asset_count: int = 2,
    rights_verified: bool = True,
) -> MediaRelease:
    bundle = root / str(run.raw_item_id)
    assets_dir = bundle / "assets"
    assets_dir.mkdir(parents=True)
    assets = []
    hashes: list[str] = []
    for index in range(asset_count):
        path = assets_dir / f"image-{index}.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes.append(digest)
        assets.append(
            {
                "file_name": path.name,
                "sha256": digest,
                "origin": "self_authored_card",
                "source_url": "https://example.org/source",
                "rights_basis": "Собственная редакционная графика",
                "rights_verified": rights_verified,
                "attribution": "Редакция",
            }
        )
    release = MediaRelease.model_validate(
        {
            "version": 1,
            "release_id": f"production-{run.raw_item_id}",
            "raw_item_id": run.raw_item_id,
            "post_id": run.post_id,
            "editorial_package_sha256": hashlib.sha256(
                canonical_json(package.model_dump(mode="json")).encode()
            ).hexdigest(),
            "target_id": str(APPROVED_CHANNEL_ID),
            "caption": caption,
            "assets": assets,
            "reviews": [
                {
                    "reviewer_id": f"{role}-reviewer",
                    "role": role,
                    "verdict": "approve",
                    "score": 90,
                    "flags": [],
                    "live_source_checked": True,
                    "checked_at": datetime.now(UTC),
                    "reviewed_asset_sha256": hashes,
                    "caption_sha256": hashlib.sha256(caption.encode()).hexdigest(),
                    "context_sha256": "0" * 64,
                    "rationale": "Независимо проверены изображение, подпись и права",
                }
                for role in ("facts", "utility")
            ],
        }
    )
    release = release.model_copy(
        update={
            "reviews": tuple(
                review.model_copy(update={"context_sha256": media_context_sha256(release)})
                for review in release.reviews
            )
        }
    )
    (bundle / "release.json").write_text(release.model_dump_json(indent=2), encoding="utf-8")
    return release


@pytest.mark.asyncio
async def test_reviewed_album_uses_normal_production_receipt(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    package = _stored_package(run)
    caption = await _preview(session, run.raw_item_id)
    media_root = tmp_path / "production-media"
    _write_release(media_root, run, package, caption)
    monkeypatch.setenv("EDITORIAL_MEDIA_RELEASE_DIR", str(media_root))
    publisher = ProductionPublisher()

    result = await deliver_reviewed(
        session,
        run.raw_item_id,
        publisher,
        target_channel_id=APPROVED_CHANNEL_ID,
        enabled=True,
    )

    post = session.get(Post, run.post_id)
    assert result.message_id == 701
    assert post is not None and post.telegram_message_id == 701
    assert publisher.text_calls == []
    assert publisher.bot.calls == [("album", APPROVED_CHANNEL_ID, caption)]
    session.refresh(run)
    receipt = run.outcome["_media_delivery"]
    assert receipt["state"] == "sent"
    assert receipt["message_ids"] == [701, 702]
    assert receipt["planned_message_count"] == 2

    again = await deliver_reviewed(
        session,
        run.raw_item_id,
        publisher,
        target_channel_id=APPROVED_CHANNEL_ID,
        enabled=True,
    )
    assert again.message_id == 701
    assert len(publisher.bot.calls) == 1


@pytest.mark.asyncio
async def test_invalid_explicit_media_bundle_blocks_before_text_fallback(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    package = _stored_package(run)
    caption = await _preview(session, run.raw_item_id)
    media_root = tmp_path / "production-media"
    _write_release(media_root, run, package, caption, asset_count=1, rights_verified=False)
    monkeypatch.setenv("EDITORIAL_MEDIA_RELEASE_DIR", str(media_root))
    publisher = ProductionPublisher()

    with pytest.raises(DeliveryBlockedError, match="media bundle"):
        await deliver_reviewed(
            session,
            run.raw_item_id,
            publisher,
            target_channel_id=APPROVED_CHANNEL_ID,
            enabled=True,
        )

    session.refresh(run)
    assert run.delivery_status == "pending"
    assert publisher.text_calls == []
    assert publisher.bot.calls == []


@pytest.mark.asyncio
async def test_unknown_media_outcome_is_uncertain_and_never_retried(
    reviewed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, run = reviewed
    package = _stored_package(run)
    caption = await _preview(session, run.raw_item_id)
    media_root = tmp_path / "production-media"
    _write_release(media_root, run, package, caption, asset_count=1)
    monkeypatch.setenv("EDITORIAL_MEDIA_RELEASE_DIR", str(media_root))
    publisher = ProductionPublisher(fail_media=True)

    with pytest.raises(TimeoutError):
        await deliver_reviewed(
            session,
            run.raw_item_id,
            publisher,
            target_channel_id=APPROVED_CHANNEL_ID,
            enabled=True,
        )
    session.refresh(run)
    assert run.delivery_status == "uncertain"
    assert run.outcome["_media_delivery"]["state"] == "uncertain"

    with pytest.raises(DeliveryBlockedError, match="reconciliation"):
        await deliver_reviewed(
            session,
            run.raw_item_id,
            publisher,
            target_channel_id=APPROVED_CHANNEL_ID,
            enabled=True,
        )
    assert len(publisher.bot.calls) == 1
