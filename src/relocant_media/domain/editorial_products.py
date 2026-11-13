"""Версионированное оформление, включённое в хеш редакционного пакета."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer


class EditorialPresentation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: int = Field(default=1, ge=1, le=3)
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rubric_id: str
    rubric_name: str
    source_map: dict[str, Any] | None = None
    illustration: dict[str, Any]
    native_map: dict[str, Any] | None = None
    event_place: dict[str, Any] | None = None

    @model_serializer(mode="wrap")
    def compatible_dump(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload: dict[str, Any] = handler(self)
        if self.native_map is None:
            payload.pop("native_map", None)
        if self.event_place is None:
            payload.pop("event_place", None)
        return payload
