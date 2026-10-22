from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from relocant_media.storage.models import Base
from relocant_media.storage.types import JSON_VALUE, UTC_DATE


class RawItemMediaSnapshot(Base):
    """Immutable-after-review source-page sidecar for editorial media discovery."""

    __tablename__ = "raw_item_media_snapshot"

    raw_item_id: Mapped[int] = mapped_column(
        ForeignKey("raw_item.id", ondelete="CASCADE"), primary_key=True
    )
    source_page: Mapped[str] = mapped_column(Text, nullable=False)
    source_html: Mapped[str | None] = mapped_column(Text)
    source_html_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_html_bytes: Mapped[int] = mapped_column(nullable=False)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSON_VALUE, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        UTC_DATE, server_default=func.now(), nullable=False
    )
