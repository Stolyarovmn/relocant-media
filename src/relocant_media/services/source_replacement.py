"""Замена недоступной статьи живым официальным первоисточником."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.collectors.base import RawItem as CollectedArticle
from relocant_media.domain.city_profile import CityEditorialProfile
from relocant_media.domain.dedup import EventCandidate, canonicalize_url, event_key
from relocant_media.domain.freshness import evidence_freshness
from relocant_media.domain.policy import CURRENT_EDITORIAL_RULE_VERSION, SOURCE_REPLACED_REASON
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.ingestion.persistence import (
    _referencing_runs,
    capture_collected_batch,
    source_is_enabled,
    sync_source,
)
from relocant_media.services.editorial import (
    _candidate,
    _document,
    _event_items,
    _reviewed_event_item,
)
from relocant_media.services.replacement_identity import matches_replacement_event
from relocant_media.sources.registry import SourceConfig
from relocant_media.storage.locks import lock_editorial_city
from relocant_media.storage.models import EditorialRun, RawItem, Source


class SourceUnavailableError(ValueError):
    """Редакция не смогла прочитать публичную страницу."""

    def __init__(self, source_id: str | None = None) -> None:
        self.source_id = source_id
        super().__init__("source_unavailable")


class SourceReplacementError(ValueError):
    """Замена источника не прошла проверку."""


def is_source_unavailable(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        code = error.response.status_code
        return code in {401, 403, 404, 410, 429} or 500 <= code <= 599
    if isinstance(error, BaseExceptionGroup):
        return all(is_source_unavailable(child) for child in error.exceptions)
    return isinstance(error, (
        SourceUnavailableError, TimeoutError, httpx.TimeoutException,
        httpx.NetworkError, httpx.RemoteProtocolError,
    ))


def _collected_candidate(item: CollectedArticle) -> EventCandidate:
    return EventCandidate(
        raw_item_id=0, source_id=item.source_id, url=item.url,
        title=item.title, body=item.body, published_at=item.published_at,
        canonical_url=item.canonical_url,
    )


def can_replace_source(
    session: Session,
    *,
    original: RawItem,
    replacement: CollectedArticle,
    config: SourceConfig,
    now: datetime,
    profile: CityEditorialProfile,
) -> bool:
    """Только связанный, актуальный и допущенный к публикации первоисточник."""
    source = session.get(Source, original.source_id, populate_existing=True)
    if (source is None or not source.enabled or source.city != profile.city_id
            or source.ownership not in {"official", "media"}
            or source.high_risk or not source.auto_publish):
        return False
    if (
        not source_is_enabled(config) or config.ownership != "official"
        or config.policy.high_risk or not config.policy.auto_publish
        or (replacement.source_id, replacement.external_id) == (
            original.source_id, original.external_id)
        or replacement.source_id != config.id
        or canonicalize_url(original.url) == canonicalize_url(replacement.url)
        or not is_public_https_url(replacement.url)
        or not replacement.title.strip() or not replacement.body.strip()
    ):
        return False
    host = urlsplit(replacement.url).hostname
    approved_hosts = {urlsplit(str(config.url)).hostname, *config.extraction.approved_hosts}
    if host is None or host.removeprefix("www.") not in {
        value.removeprefix("www.") for value in approved_hosts if value is not None
    }:
        return False
    registered = session.get(Source, config.id, populate_existing=True)
    if registered is not None and (
        registered.city != profile.city_id or not registered.enabled
        or registered.high_risk or not registered.auto_publish
        or registered.ownership != "official" or registered.source_type != config.source_type
    ):
        return False
    if any(policy.high_risk for _, policy in _event_items(
        session, anchor=original, anchor_source=source,
    )):
        return False
    return (
        matches_replacement_event(_candidate(original), _collected_candidate(replacement))
        and evidence_freshness(
            replacement.title, replacement.body, replacement.published_at, now,
            precision=replacement.published_at_precision, source_type=config.source_type,
            verified_at=None, timezone_name=profile.timezone,
        ).action == "keep"
    )


def record_source_replacement(
    session: Session,
    *,
    original: RawItem,
    replacement: CollectedArticle,
    config: SourceConfig,
    now: datetime,
    profile: CityEditorialProfile,
) -> int:
    """Сохранить самостоятельное доказательство и связать его со старой темой."""
    original_document = _document(original)
    lock_editorial_city(session, profile.city_id)
    current = session.get(RawItem, original.id, populate_existing=True)
    if current is None or _document(current) != original_document:
        raise SourceReplacementError("Снимок исходной темы изменился")
    if not can_replace_source(
        session, original=current, replacement=replacement, config=config, now=now, profile=profile,
    ):
        raise SourceReplacementError("Официальная замена не прошла проверку")
    original_source = session.get(Source, current.source_id)
    assert original_source is not None
    if (_referencing_runs(session, current.id)
            or _reviewed_event_item(session, anchor=current, anchor_source=original_source)):
        raise SourceReplacementError("Исходная тема уже проверена")
    existing = session.scalar(select(RawItem).where(
        RawItem.source_id == replacement.source_id, RawItem.external_id == replacement.external_id,
    ))
    if existing is not None:
        previous_replacement = session.scalar(select(EditorialRun.raw_item_id).where(
            EditorialRun.outcome["reason"].as_string() == SOURCE_REPLACED_REASON,
            EditorialRun.outcome["replacement_raw_item_id"].as_integer() == existing.id,
        ).limit(1))
        if previous_replacement is not None or _referencing_runs(session, existing.id):
            raise SourceReplacementError("Официальный первоисточник уже связан с заданием")
    sync_source(session, profile.city_id, config)
    if capture_collected_batch(session, [replacement]) != (1, 0):
        raise SourceReplacementError("Официальный источник изменился во время записи")
    target = session.scalar(select(RawItem).where(
        RawItem.source_id == replacement.source_id, RawItem.external_id == replacement.external_id,
    ).execution_options(populate_existing=True))
    target_source = session.get(Source, config.id, populate_existing=True)
    assert target is not None and target_source is not None
    if _reviewed_event_item(session, anchor=target, anchor_source=target_source):
        raise SourceReplacementError("Тема официального источника уже проверена")
    session.add(EditorialRun(
        raw_item_id=current.id,
        outcome={"status": "skipped", "reason": SOURCE_REPLACED_REASON,
                 "replacement_raw_item_id": target.id, "ai_calls": 0},
        evidence={"raw_item_id": current.id, "source_id": current.source_id,
                  "event_key": event_key(_candidate(current))},
        delivery_status="pending",
        editorial_rule_version=CURRENT_EDITORIAL_RULE_VERSION,
    ))
    session.flush()
    return target.id
