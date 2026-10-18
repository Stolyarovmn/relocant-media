from datetime import date
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, Field, HttpUrl


class ExtractionConfig(BaseModel):
    strategy: str
    status: str | None = None


class SourcePolicy(BaseModel):
    high_risk: bool = False
    auto_publish: bool = True


class VerificationMetadata(BaseModel):
    checked_at: date | None = None
    note: str | None = None


class SourceConfig(BaseModel):
    id: str
    name: str
    publisher: str
    ownership: str
    url: HttpUrl
    language: str
    source_type: str
    trust: float = Field(ge=0.0, le=1.0)
    check_every: str
    categories: tuple[str, ...]
    extraction: ExtractionConfig
    policy: SourcePolicy = Field(default_factory=SourcePolicy)
    verification: VerificationMetadata = Field(default_factory=VerificationMetadata)


def load_source_file(path: Path) -> SourceConfig:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"source config must be a mapping: {path}")
    return SourceConfig.model_validate(cast(dict[str, Any], raw))


def load_source_registry(directory: Path) -> tuple[SourceConfig, ...]:
    configs = tuple(load_source_file(path) for path in sorted(directory.glob("*.yaml")))
    ids = [source.id for source in configs]
    duplicates = sorted(source_id for source_id in set(ids) if ids.count(source_id) > 1)
    if duplicates:
        raise ValueError(f"duplicate source ids: {', '.join(duplicates)}")
    return configs
