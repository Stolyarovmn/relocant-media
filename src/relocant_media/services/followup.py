"""Связь подтверждённого изменения с исходной отдельной публикацией."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import EditorialRun, Post
from relocant_media.storage.revision_control import SourceRevisionMaterialization


def revision_reply_target(session: Session, raw_item_id: int, city: str) -> int | None:
    parent = session.scalar(select(SourceRevisionMaterialization.parent_raw_item_id).where(
        SourceRevisionMaterialization.raw_item_id == raw_item_id,
    ))
    if parent is None:
        return None
    return session.scalar(select(Post.telegram_message_id).join(
        EditorialRun, EditorialRun.post_id == Post.id,
    ).where(EditorialRun.raw_item_id == parent, EditorialRun.delivery_status == "sent",
            Post.city == city, Post.telegram_message_id > 0))
