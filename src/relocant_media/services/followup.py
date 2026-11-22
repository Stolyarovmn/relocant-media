"""Связь подтверждённого изменения с исходной отдельной публикацией."""

import json
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import EditorialRun, Post
from relocant_media.storage.revision_control import SourceRevisionMaterialization


def revision_reply_target(
    session: Session, raw_item_id: int, city: str, *, now: datetime | None = None,
) -> int | None:
    run = session.get(EditorialRun, raw_item_id, populate_existing=True)
    payload = run.outcome.get("_package") if run is not None else None
    if isinstance(payload, dict) and payload.get("linked_update") is not None:
        from relocant_media.ai.package import EditorialPackage
        from relocant_media.services.linked_updates import validate_linked_update

        package = EditorialPackage.model_validate_json(json.dumps(payload))
        if package.city != city or package.raw_item_id != raw_item_id:
            raise ValueError("Идентичность связанного обновления изменилась")
        validate_linked_update(session, package, now=now or datetime.now(UTC))
        assert package.linked_update is not None
        return package.linked_update.parent.message_id
    parent = session.scalar(select(SourceRevisionMaterialization.parent_raw_item_id).where(
        SourceRevisionMaterialization.raw_item_id == raw_item_id,
    ))
    if parent is None:
        return None
    return session.scalar(select(Post.telegram_message_id).join(
        EditorialRun, EditorialRun.post_id == Post.id,
    ).where(EditorialRun.raw_item_id == parent, EditorialRun.delivery_status == "sent",
            Post.city == city, Post.telegram_message_id > 0))
