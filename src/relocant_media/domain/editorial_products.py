"""Версионированное оформление, включённое в хеш редакционного пакета."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EditorialPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(default=1, ge=1, le=1)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_id: str
    rubric_name: str
    source_map: dict[str, Any] | None = None
    illustration: dict[str, Any]
