"""Trusted cloud discovery pointers enter the existing Submission queue, never evidence.

The caller must hold the canonical writer claim for the entire transaction. No Telegram
identities, source policies, article dates, drafts or review verdicts are accepted as input.
"""
from __future__ import annotations

import hashlib
import ipaddress
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.domain.dedup import canonicalize_url
from relocant_media.storage.models import EditorialRun, Submission

ORIGIN = "cloud_discovery"
MAX_BATCH_BYTES = 24_000


class DiscoveryBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    version: Literal[1]
    city: Literal["belgrade"]
    urls: list[str] = Field(min_length=1, max_length=10)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("integer version required")
        return value

    @field_validator("urls")
    @classmethod
    def public_urls(cls, urls: list[str]) -> list[str]:
        for url in urls:
            if (len(url) > 2_000 or any(c.isspace() or ord(c) < 32 for c in url)
                    or any(c in url for c in "\\<>\"'")):
                raise ValueError("public HTTPS URL required")
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or not host or "." not in host
                    or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, 443)
                    or host.endswith((".localhost", ".local", ".internal"))):
                raise ValueError("public HTTPS URL required")
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                if not address.is_global:
                    raise ValueError("public HTTPS URL required")
        return urls


def parse_batch(value: str) -> DiscoveryBatch:
    if len(value.encode("utf-8")) > MAX_BATCH_BYTES:
        raise ValueError("discovery batch exceeds bound")
    return DiscoveryBatch.model_validate_json(value)


def candidate_key(city: str, url: str) -> str:
    return hashlib.sha256(f"{city}\n{canonicalize_url(url)}".encode()).hexdigest()


def ingest_batch(session: Session, batch: DiscoveryBatch) -> tuple[list[int], int]:
    """Idempotent under the caller's canonical writer claim; commit belongs to caller."""
    ids: list[int] = []
    created = 0
    for url in batch.urls:
        key = candidate_key(batch.city, url)
        row = session.scalar(select(Submission).where(
            Submission.payload["origin"].as_string() == ORIGIN,
            Submission.payload["candidate_key"].as_string() == key,
        ))
        if row is None:
            row = Submission(kind="news_tip", status="new", payload={
                "city": batch.city, "origin": ORIGIN, "candidate_key": key,
                "text": url, "discovered_at": datetime.now(UTC).isoformat(),
                "publication_evidence": False,
            })
            session.add(row)
            session.flush()
            created += 1
        if row.id not in ids:
            ids.append(row.id)
    return ids, created


def batch_receipt(session: Session, ids: list[int]) -> list[dict[str, Any]]:
    """Observe stored outcomes without inferring publication from a successful worker exit."""
    results: list[dict[str, Any]] = []
    for sid in ids:
        row = session.get(Submission, sid)
        if row is None or row.payload.get("origin") != ORIGIN:
            raise ValueError("cloud submission missing from canonical checkpoint")
        raw_ids = [v for v in row.payload.get("raw_item_ids", []) if type(v) is int]
        runs = list(session.scalars(select(EditorialRun).where(
            EditorialRun.raw_item_id.in_(raw_ids)))) if raw_ids else []
        results.append({
            "submission_id": sid, "candidate_key": row.payload["candidate_key"],
            "intake_status": row.status, "reason": row.payload.get("triage_reason"),
            "raw_item_ids": raw_ids,
            "deliveries": [{"raw_item_id": run.raw_item_id,
                            "status": run.delivery_status,
                            "message_id": run.staging_message_id} for run in runs],
        })
    return results
