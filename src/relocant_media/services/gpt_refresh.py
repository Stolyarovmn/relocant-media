"""Пересъём публичных доказательств для одного задания подписочного резерва."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from typing import Any, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from pydantic import Field, field_validator, model_validator
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.ai.channel_style import writing_brief
from relocant_media.ai.package import StrictModel, canonical_json
from relocant_media.collectors.base import RawItem as CollectedArticle
from relocant_media.collectors.linked_tour_map import TourMapUnavailable
from relocant_media.collectors.ticket_vision_events import TicketCardUnavailable
from relocant_media.domain.dedup import EventCandidate, canonicalize_url, same_event
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.editorial import _candidate, _reviewed_event_item
from relocant_media.services.gpt_fallback import (
    REQUEST_TTL,
    FallbackValidationError,
    _bounded_json,
    _candidate_request,
    _candidate_respond_by,
    _public_host,
    _registry,
    refresh_target,
    validate_fallback_candidate,
)
from relocant_media.services.material_lifecycle import (
    FALLBACK_WAIT,
    archive_for_retry,
    authorize_refreshed_evidence,
    can_retry_snapshot,
    inherit_lifecycle,
)
from relocant_media.services.primary_discovery import source_for_url
from relocant_media.services.source_replacement import (
    SourceUnavailableError,
    can_replace_source,
    is_source_unavailable,
    record_source_replacement,
)
from relocant_media.services.venue_event_evidence import matches_venue_evidence, named_venue_sources
from relocant_media.sources.registry import SourceConfig
from relocant_media.storage.models import EditorialRun, RawItem, Source

MAX_ALTERNATIVES = 3
MAX_REFRESHES = 2
NO_MATCHING_REFRESH_SOURCE = "refresh_no_matching_source"


class RefreshResult(StrictModel):
    version: Literal[3]
    kind: Literal["evidence_refresh"]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_item_id: int = Field(gt=0, lt=2**63)
    urls: list[str] = Field(max_length=MAX_ALTERNATIVES)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Версия результата должна быть целым числом")
        return value

    @field_validator("urls")
    @classmethod
    def public_urls(cls, urls: list[str]) -> list[str]:
        if not all(len(url) <= 2048 and _public_host(url) is not None
                   and is_public_https_url(url) for url in urls):
            raise ValueError("Нужны публичные HTTPS-адреса")
        if len({canonicalize_url(url) for url in urls}) != len(urls):
            raise ValueError("Повтор адреса первоисточника")
        return urls


class RefreshInput(StrictModel):
    request: dict[str, Any]
    result: RefreshResult

    @model_validator(mode="after")
    def bound_request(self) -> Self:
        if self.result.request_sha256 != _digest(self.request):
            raise ValueError("Обновление не соответствует исходному запросу")
        return self


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _registered_alternative(url: str, registry: dict[str, SourceConfig]) -> SourceConfig:
    host = _public_host(url)
    matching = [
        config for config in registry.values()
        if config.extraction.status in {None, "active"}
        and config.ownership in {"official", "media"}
        and host is not None
        and host in {
            _public_host(str(config.url)),
            *(item.removeprefix("www.") for item in config.extraction.approved_hosts),
        }
    ]
    if len(matching) > 1:
        match_by_url = source_for_url(url, {config.id: config for config in matching})
        if match_by_url is not None:
            return registry[match_by_url]
        path = urlsplit(url).path
        by_root = [
            (len(root), config)
            for config in matching
            if (root := urlsplit(str(config.url)).path.rstrip("/"))
            and (path == root or path.startswith(root + "/"))
        ]
        if by_root:
            longest = max(length for length, _ in by_root)
            matching = [config for length, config in by_root if length == longest]
    if len(matching) != 1:
        raise FallbackValidationError("Альтернативный источник не зарегистрирован однозначно")
    return matching[0]


async def _collect(
    links: list[tuple[SourceConfig, str]],
) -> list[CollectedArticle | SourceUnavailableError]:
    from relocant_media.ops.server_cycle import fetch_public_article

    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        async def collect(
            config: SourceConfig, url: str,
        ) -> CollectedArticle | SourceUnavailableError:
            try:
                result = await fetch_public_article(config, url, client)
            except Exception as error:
                if (isinstance(error, (TicketCardUnavailable, TourMapUnavailable))
                        or is_source_unavailable(error)):
                    return SourceUnavailableError(config.id)
                raise
            if (not isinstance(result, CollectedArticle) or result.source_id != config.id
                    or canonicalize_url(result.url) != canonicalize_url(url)):
                raise FallbackValidationError("Сборщик вернул другой источник или адрес")
            return result

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(collect(config, url)) for config, url in links]
        return [task.result() for task in tasks]


def _collected_candidate(item: CollectedArticle) -> EventCandidate:
    return EventCandidate(
        raw_item_id=0, source_id=item.source_id, url=item.url, title=item.title,
        body=item.body, published_at=item.published_at, canonical_url=item.canonical_url,
    )


def _refreshes(run: SchedulerRun) -> dict[str, Any]:
    current: object = run.details.get("gpt_evidence_refresh", {})
    if not isinstance(current, dict):
        raise FallbackValidationError("Некорректная квитанция пересъёма")
    return current


def refreshed_anchor_sha256(run: SchedulerRun, raw_id: int) -> str | None:
    receipt = _refreshes(run).get(str(raw_id))
    if receipt is None:
        return None
    digest = receipt.get("anchor_sha256") if isinstance(receipt, dict) else None
    if not isinstance(digest, str):
        raise FallbackValidationError("Некорректный хеш переснятого исходника")
    return digest


def _refresh_origin_id(session: Session, run: SchedulerRun, raw_id: int) -> int:
    origins = [
        int(key) for key, receipt in _refreshes(run).items()
        if key.isdecimal() and isinstance(receipt, dict)
        and receipt.get("replacement_raw_item_id") == raw_id
        and refresh_target(session, run, int(key)) == raw_id
    ]
    if len(origins) > 1:
        raise FallbackValidationError("Найдены разные исходные задания замены")
    return origins[0] if origins else raw_id


def _require_unresolved(session: Session, raw_id: int, *,
                        now: datetime | None = None) -> tuple[RawItem, Source]:
    from relocant_media.services.material_lifecycle import (
        LifecycleBlockedError,
        ensure_not_terminal,
    )

    now = now or datetime.now(UTC)
    try:
        ensure_not_terminal(session, raw_id, now=now)
    except LifecycleBlockedError as error:
        raise FallbackValidationError(str(error), code="material_closed") from error
    raw = session.get(RawItem, raw_id, populate_existing=True)
    source = session.get(Source, raw.source_id) if raw is not None else None
    existing = session.get(EditorialRun, raw_id, populate_existing=True)
    if (raw is None or source is None
            or existing is not None and not can_retry_snapshot(session, raw_id, now=now)
            or _reviewed_event_item(session, anchor=raw, anchor_source=source) is not None):
        raise FallbackValidationError(
            "Материал уже имеет редакционное решение", code="material_closed",
        )
    return raw, source


def refresh_gpt_evidence(
    engine: Engine, payload: RefreshInput, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Actions перечитывает материал и добавляет связанные документы; публикации нет."""
    raw_id = payload.result.raw_item_id
    registry = _registry()
    with Session(engine) as session:
        parsed, profile, candidate, _ = validate_fallback_candidate(
            session, payload.request, raw_id, now=now or datetime.now(UTC),
        )
        raw, source = _require_unresolved(session, raw_id, now=now)
        run = session.get(SchedulerRun, parsed.scheduler_run_id)
        assert run is not None
        origin_id = _refresh_origin_id(session, run, raw_id)
        receipt = _refreshes(run).get(str(origin_id), {})
        attempts = receipt.get("attempts", 0) if isinstance(receipt, dict) else None
        if (not isinstance(receipt, dict) or type(attempts) is not int
                or not 0 <= attempts < MAX_REFRESHES):
            raise FallbackValidationError(
                "Лимит пересъёмов для этого задания достигнут", code="refresh_limit_reached",
            )
        if receipt.get("request_sha256") == payload.result.request_sha256:
            raise FallbackValidationError(
                "Этот запрос на пересъём уже обработан", code="refresh_already_applied",
            )
        existing = [(registry[doc.source_id], doc.url) for doc in candidate.documents]
        seen = {canonicalize_url(url) for _, url in existing}
        alternatives = [
            (_registered_alternative(url, registry), url)
            for url in payload.result.urls if canonicalize_url(url) not in seen
        ]
        planned = existing + alternatives
        if len({source.id for source, _ in planned}) != len(planned):
            raise FallbackValidationError("Нужен один документ от каждого источника")
        anchor = _candidate(raw)
        venue_sources = named_venue_sources(raw, source)
        published = (raw.published_at.astimezone(ZoneInfo(profile.timezone)).date()
                     if raw.published_at is not None else None)

    def matches(item: CollectedArticle) -> bool:
        venue = venue_sources.get(item.source_id)
        return same_event(anchor, _collected_candidate(item)) or bool(
            venue is not None and published is not None
            and matches_venue_evidence(raw, item, venue, published=published)
        )

    # Внешнее подпроцессное объявление URL не становится доказательством до сбора.
    # Надёжная связь источников берётся из тех же правил, что и обычная редакционная пачка.
    collected = asyncio.run(_collect(planned))
    if len(collected) != len(planned):
        raise FallbackValidationError("Сборщик не вернул результаты для всех адресов")
    original_unavailable = isinstance(collected[0], SourceUnavailableError)
    articles = [item for item in collected if isinstance(item, CollectedArticle)]
    verified_alternatives = []
    if not original_unavailable:
        existing_articles = [item for item in collected[:len(existing)]
                             if isinstance(item, CollectedArticle)]
        if len(existing_articles) != len(existing) or any(
            not matches(item) for item in existing_articles
        ):
            raise FallbackValidationError(
                "Собранный снимок исходных источников не совпадает с заданием",
                code="refresh_existing_evidence_changed",
            )
        verified_alternatives = [item for item in collected[len(existing):]
                                 if isinstance(item, CollectedArticle)
                                 and matches(item)]
        if alternatives and not verified_alternatives:
            raise FallbackValidationError(
                "Новые источники не подтвердили то же событие", code=NO_MATCHING_REFRESH_SOURCE,
            )
        articles = existing_articles + verified_alternatives
    with Session(engine) as session:
        now = now or datetime.now(UTC)
        validate_fallback_candidate(session, payload.request, raw_id, now=now)
        raw, _ = _require_unresolved(session, raw_id, now=now)
        # Сохраняем прежний безопасный отказ только в атомарной фазе записи.
        # Проверки до сети не меняют историю; rollback не теряет старый снимок.
        if (session.get(EditorialRun, raw_id) is not None
                and not archive_for_retry(session, raw_id, now=now)):
            raise FallbackValidationError("Прежний снимок нельзя безопасно архивировать")
        target_id = raw_id
        if original_unavailable:
            if origin_id != raw_id:
                raise FallbackValidationError(
                    "Ранее выбранный первоисточник недоступен", code="source_unavailable",
                )
            replacement = next((item for item in articles if can_replace_source(
                session, original=raw, replacement=item, config=registry[item.source_id],
                now=now, profile=profile,
            )), None)
            if replacement is None:
                raise FallbackValidationError(
                    "Нет доступного проверенного первоисточника события", code="source_unavailable",
                )
            target_id = record_source_replacement(
                session, original=raw, replacement=replacement,
                config=registry[replacement.source_id], now=now, profile=profile,
            )
            inherit_lifecycle(session, origin_id, target_id, now=now)
            articles = [replacement]
        else:
            if (articles[0].source_id, articles[0].external_id) != (raw.source_id, raw.external_id):
                raise FallbackValidationError("Сборщик вернул новый ID исходного материала")
            for item in verified_alternatives:
                sync_source(session, profile.city_id, registry[item.source_id])
            written, revisions = capture_collected_batch(session, articles)
            if written != len(articles) or revisions:
                raise FallbackValidationError("Пересъём затронул ранее проверенный снимок")
            if not authorize_refreshed_evidence(session, raw_id, now=now):
                raise FallbackValidationError("Жизненный цикл не допускает обновление снимка")
        refreshed = _candidate_request(
            session, target_id, profile=profile, registry=registry, now=now,
        )
        all_urls = {canonicalize_url(doc["url"]) for doc in refreshed["documents"]}
        if any(canonicalize_url(item.url) not in all_urls for item in articles):
            raise FallbackValidationError("Сборщик не связал все документы с исходным событием")
        run = session.get(SchedulerRun, parsed.scheduler_run_id)
        assert run is not None
        request_end = now + REQUEST_TTL
        if parsed.batch_result_sha256 is not None:
            assert run.finished_at is not None
            request_end = min(request_end, run.finished_at + FALLBACK_WAIT)
        refreshed["respond_by"] = _candidate_respond_by(
            session, target_id, request_end=request_end,
        )
        old = _refreshes(run)
        current_anchor = next(doc for doc in refreshed["documents"]
                              if doc["raw_item_id"] == target_id)
        replacement_scope = ({"replacement_raw_item_id": target_id}
                             if target_id != origin_id else {})
        run.details = {**run.details, "gpt_evidence_refresh": {**old, str(origin_id): {
            "attempts": attempts + 1,
            "request_sha256": payload.result.request_sha256,
            "anchor_sha256": _digest(current_anchor),
            "sources": len(refreshed["documents"]),
            **replacement_scope,
        }}}
        request = {**payload.request, "candidates": [refreshed], "created_at": now.isoformat(),
                   "writing_brief": writing_brief(profile, now)}
        _bounded_json(request)
        # Прогнать тот же валидатор, который позже примет независимо проверенный пакет.
        session.flush()
        validate_fallback_candidate(session, request, target_id, now=now)
        session.commit()
    return request
