from datetime import time
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, Field, field_validator


class OperationsConfig(BaseModel):
    daily_report_local_time: time = time(hour=0, minute=15)
    channel_snapshot_every: str = "1h"

    @field_validator("daily_report_local_time", mode="before")
    @classmethod
    def parse_time(cls, value: object) -> object:
        if isinstance(value, str):
            return time.fromisoformat(value)
        return value

    @field_validator("channel_snapshot_every")
    @classmethod
    def validate_channel_snapshot_interval(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("channel_snapshot_every must not be empty")
        return normalized


class CityOperationsConfig(BaseModel):
    id: str
    timezone: str
    operations: OperationsConfig = Field(default_factory=OperationsConfig)


def load_city_operations(path: Path) -> CityOperationsConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"city config must be a mapping: {path}")
    return CityOperationsConfig.model_validate(cast(dict[str, Any], raw))
