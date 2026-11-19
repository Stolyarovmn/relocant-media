"""Подготовить и проверить резерв пакетного отбора на каноническом снимке."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.batch_preselection import MAX_CANDIDATES as BATCH_CAPACITY
from relocant_media.ai.channel_style import writing_brief
from relocant_media.ai.package import StrictModel, canonical_json
from relocant_media.domain.city_profile import CityEditorialProfile, load_city_profile
from relocant_media.domain.freshness import evidence_freshness
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.editorial import _reviewed_event_item, _snapshot_member
from relocant_media.services.gpt_fallback import (
    MAX_CANDIDATES,
    REQUEST_TTL,
    FallbackValidationError,
    _bounded_json,
    _candidate_request,
    _recent_runs,
    _registered_public,
    _registry,
    _utc,
    build_request,
)
from relocant_media.sources.registry import SourceConfig
from relocant_media.storage.models import EditorialRun, RawItem, Source

_Id = Annotated[int, Field(gt=0, lt=2**63)]
_Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_SHA256 = re.compile(r"[0-9a-f]{64}")


class _Brief(StrictModel):
    raw_item_id: _Id
    source_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=180)
    body_excerpt: str = Field(min_length=1, max_length=900)
    published_at: AwareDatetime
    evidence_sha256: _Digest


class _BatchRequest(StrictModel):
    version: Literal[2]
    kind: Literal["batch_preselection"]
    scheduler_run_id: _Id
    created_at: AwareDatetime
    city_profile: CityEditorialProfile
    profile_sha256: _Digest
    max_selected: int = Field(ge=1, le=MAX_CANDIDATES)
    candidates: list[_Brief] = Field(min_length=1, max_length=BATCH_CAPACITY)


class _BatchRank(StrictModel):
    version: Literal[2]
    kind: Literal["batch_preselection"]
    request_sha256: _Digest
    selected_ids: list[_Id] = Field(max_length=MAX_CANDIDATES)


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _ids(value: object, limit: int) -> list[int] | None:
    if not isinstance(value, list) or len(value) > limit:
        return None
    if any(type(raw_id) is not int or not 0 < raw_id < 2**63 for raw_id in value):
        return None
    if len(set(value)) != len(value):
        return None
    return value


def _batch_state(
    run: SchedulerRun | None, now: datetime,
) -> tuple[dict[str, Any], list[int], int] | None:
    if (
        run is None
        or run.task_key != "editorial-primary:belgrade"
        or run.task_type != "editorial_primary"
        or run.city != "belgrade"
        or run.status not in {"partial", "failed"}
        or run.finished_at is None
        or run.started_at > run.finished_at
        or not timedelta() <= now - run.finished_at <= REQUEST_TTL
    ):
        return None
    details = run.details.get("batch_preselection")
    if not isinstance(details, dict) or details.get("status") not in {"gpt_waiting", "gpt_ranked"}:
        return None
    slots = details.get("gpt_slots")
    candidates = _ids(details.get("gpt_candidate_ids"), BATCH_CAPACITY)
    if type(slots) is not int or not 1 <= slots <= MAX_CANDIDATES or not candidates:
        return None
    processed = details.get("full_review_ids", [])
    if _ids(processed, MAX_CANDIDATES) is None or len(processed) + slots > MAX_CANDIDATES:
        return None
    if set(processed) & set(candidates):
        return None
    if details["status"] == "gpt_ranked":
        selected = _ids(details.get("gpt_selected_ids"), slots)
        evidence = details.get("gpt_evidence_sha256")
        if (
            selected is None
            or not set(selected).issubset(candidates)
            or not _is_digest(details.get("gpt_rank_sha256"))
            or not _is_digest(details.get("gpt_request_sha256"))
            or not isinstance(evidence, dict)
            or set(evidence) != {str(raw_id) for raw_id in selected}
            or not all(_is_digest(digest) for digest in evidence.values())
        ):
            return None
    return details, candidates, slots


def _brief(
    item: RawItem, source: Source, *, now: datetime, profile: CityEditorialProfile,
    registry: dict[str, SourceConfig],
) -> dict[str, Any] | None:
    if (
        not _registered_public(item, source, registry.get(source.id))
        or source.ownership not in {"official", "media"}
        or not item.title.strip()
        or not item.body.strip()
        or item.published_at is None
        or evidence_freshness(
            item.title, item.body, item.published_at, now,
            precision=item.published_at_precision, source_type=source.source_type,
            verified_at=item.ingested_at, timezone_name=profile.timezone,
        ).action != "keep"
    ):
        return None
    snapshot = _snapshot_member(item, source)
    return {
        "raw_item_id": item.id,
        "source_id": source.id,
        "title": item.title[:180],
        "body_excerpt": item.body[:900],
        "published_at": snapshot["published_at"],
        "evidence_sha256": _digest(snapshot),
    }


def _briefs(
    session: Session,
    raw_ids: list[int],
    *,
    now: datetime,
    profile: CityEditorialProfile,
    registry: dict[str, SourceConfig],
) -> dict[int, dict[str, Any]]:
    if not raw_ids:
        return {}
    rows = session.execute(
        select(RawItem, Source).join(Source)
        .outerjoin(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
        .where(RawItem.id.in_(raw_ids), EditorialRun.raw_item_id.is_(None))
        .execution_options(populate_existing=True)
    )
    return {
        item.id: brief
        for item, source in rows
        if (brief := _brief(item, source, now=now, profile=profile, registry=registry))
        is not None
    }


def pending_batch_ids(session: Session, *, now: datetime) -> set[int]:
    """Оставить пакету его нерешённые темы только пока действует срок резерва."""
    from relocant_media.services.gpt_refresh import refreshed_anchor_sha256

    now = _utc(now)
    with session.no_autoflush:
        waiting: set[int] = set()
        ranked_hashes: dict[int, set[str]] = {}
        for run in _recent_runs(session, now):
            state = _batch_state(run, now)
            if state is None:
                continue
            details, candidates, _ = state
            if details["status"] == "gpt_ranked":
                for raw_id in details["gpt_selected_ids"]:
                    digest = (refreshed_anchor_sha256(run, raw_id)
                              or details["gpt_evidence_sha256"][str(raw_id)])
                    ranked_hashes.setdefault(raw_id, set()).add(digest)
            else:
                waiting.update(candidates)
        ids = waiting | ranked_hashes.keys()
        if not ids:
            return set()
        available = _briefs(
            session, list(ids), now=now, profile=load_city_profile("belgrade"),
            registry=_registry(),
        )
        return {
            raw_id for raw_id, brief in available.items()
            if raw_id in waiting or brief["evidence_sha256"] in ranked_hashes.get(raw_id, set())
        }


def build_batch_request(
    session: Session, *, now: datetime, scheduler_run_id: int | None = None,
) -> dict[str, Any] | None:
    """Экспортировать только краткие фрагменты одной незавершённой публичной пачки."""
    now = _utc(now)
    with session.no_autoflush:
        runs = (
            _recent_runs(session, now) if scheduler_run_id is None else
            [session.get(SchedulerRun, scheduler_run_id, populate_existing=True)]
        )
        profile = load_city_profile("belgrade")
        registry = _registry()
        for run in runs:
            state = _batch_state(run, now)
            if run is None or state is None:
                continue
            details, candidates, slots = state
            if details["status"] != "gpt_waiting":
                continue
            available = _briefs(session, candidates, now=now, profile=profile, registry=registry)
            if not available:
                continue
            request = {
                "version": 2,
                "kind": "batch_preselection",
                "scheduler_run_id": run.id,
                "created_at": now.isoformat(),
                "city_profile": profile.model_dump(mode="json"),
                "profile_sha256": profile.sha256,
                "max_selected": min(slots, len(available)),
                "candidates": [available[raw_id] for raw_id in candidates if raw_id in available],
            }
            _BatchRequest.model_validate_json(_bounded_json(request))
            return request
    return None


def validate_batch_rank(
    session: Session, batch_request: dict[str, Any], batch_result: dict[str, Any], *, now: datetime,
) -> tuple[SchedulerRun, list[int]]:
    """Привязать выбор GPT к точной пачке; повтор той же квитанции безопасен."""
    now = _utc(now)
    if any(type(value.get("version")) is not int for value in (batch_request, batch_result)):
        raise FallbackValidationError("Некорректная версия резервного отбора")
    request = _BatchRequest.model_validate_json(_bounded_json(batch_request))
    rank = _BatchRank.model_validate_json(_bounded_json(batch_result))
    if rank.request_sha256 != _digest(batch_request):
        raise FallbackValidationError("Выбор GPT не соответствует исходной пачке")
    if not timedelta() <= now - request.created_at <= REQUEST_TTL:
        raise FallbackValidationError("Запрос резервного отбора просрочен или датирован будущим")
    profile = load_city_profile("belgrade")
    if (
        request.profile_sha256 != profile.sha256
        or batch_request["city_profile"] != profile.model_dump(mode="json")
    ):
        raise FallbackValidationError("Профиль города изменился после запроса")
    with session.no_autoflush:
        run = session.get(SchedulerRun, request.scheduler_run_id, populate_existing=True)
        state = _batch_state(run, now)
        if run is None or state is None or run.finished_at is None:
            raise FallbackValidationError("Исходный сбой пакетного отбора больше не подтверждён")
        details, candidates, slots = state
        briefs = {brief.raw_item_id: brief.model_dump(mode="json") for brief in request.candidates}
        selected = rank.selected_ids
        if (
            request.created_at < run.finished_at
            or len(briefs) != len(request.candidates)
            or list(briefs) != [candidate for candidate in candidates if candidate in briefs]
            or request.max_selected != min(slots, len(briefs))
            or len(selected) > request.max_selected
            or len(set(selected)) != len(selected)
            or not set(selected).issubset(briefs)
        ):
            raise FallbackValidationError("Выбор GPT выходит за пределы исходной пачки")
        if details["status"] == "gpt_ranked":
            evidence = {str(raw_id): briefs[raw_id]["evidence_sha256"] for raw_id in selected}
            if (
                details["gpt_selected_ids"] != selected
                or details["gpt_rank_sha256"] != _digest(batch_result)
                or details["gpt_request_sha256"] != rank.request_sha256
                or details["gpt_evidence_sha256"] != evidence
            ):
                raise FallbackValidationError("Для цикла уже принят другой порядок GPT")
            # Прежняя квитанция закрепила выбор; последующий импорт сам сверяет
            # каждый ещё не обработанный материал с его исходным снимком.
            return run, selected
        registry = _registry()
        canonical = _briefs(
            session, candidates, now=request.created_at, profile=profile, registry=registry,
        )
        current = _briefs(session, selected, now=now, profile=profile, registry=registry)
        if briefs != canonical or any(current.get(raw_id) != briefs[raw_id] for raw_id in selected):
            raise FallbackValidationError("Сведения в резервной пачке изменились после её чтения")
        return run, selected


def build_ranked_request(
    session: Session, batch_request: dict[str, Any], batch_result: dict[str, Any], *, now: datetime,
) -> dict[str, Any] | None:
    """Экспортировать полные публичные исходники только для принятых тем конкретного цикла."""
    from relocant_media.services.gpt_refresh import refreshed_anchor_sha256

    now = _utc(now)
    with session.no_autoflush:
        run, selected = validate_batch_rank(session, batch_request, batch_result, now=now)
        if run.details["batch_preselection"]["status"] != "gpt_ranked":
            raise FallbackValidationError("Ранжирование GPT ещё не принято")
        profile = load_city_profile("belgrade")
        registry = _registry()
        current = _briefs(session, selected, now=now, profile=profile, registry=registry)
        original = {brief["raw_item_id"]: brief for brief in batch_request["candidates"]}
        candidates: list[dict[str, Any]] = []
        request = {
            "version": 1,
            "scheduler_run_id": run.id,
            "created_at": now.isoformat(),
            "city_profile": profile.model_dump(mode="json"),
            "profile_sha256": profile.sha256,
            "writing_brief": writing_brief(profile, now),
            "batch_request_sha256": _digest(batch_request),
            "batch_result_sha256": _digest(batch_result),
            "candidates": candidates,
        }
        # Защищённые темы уже заняли часть исходного лимита. Если полный
        # разбор у Gemini тоже упал, дать их GPT вместе с выбранными темами.
        protected_ids = set(run.details["batch_preselection"].get("full_review_ids", []))
        if protected_ids:
            protected = build_request(session, now=now, scheduler_run_id=run.id)
            if protected is not None:
                candidates.extend(candidate for candidate in protected["candidates"]
                                  if candidate["raw_item_id"] in protected_ids)
                # Конверт с привязкой к пакету немного больше обычного.
                while candidates:
                    try:
                        _bounded_json(request)
                        break
                    except ValueError:
                        candidates.pop()
        for raw_id in selected:
            if raw_id not in current:
                continue
            refreshed = refreshed_anchor_sha256(run, raw_id)
            if (current[raw_id]["evidence_sha256"] != refreshed if refreshed is not None
                    else current[raw_id] != original[raw_id]):
                continue
            item = session.get(RawItem, raw_id, populate_existing=True)
            assert item is not None
            source = session.get(Source, item.source_id, populate_existing=True)
            assert source is not None
            if _reviewed_event_item(session, anchor=item, anchor_source=source) is not None:
                continue
            try:
                candidate = _candidate_request(
                    session, raw_id, profile=profile, registry=registry, now=now,
                )
            except ValueError:
                continue
            candidates.append(candidate)
            try:
                _bounded_json(request)
            except ValueError:
                candidates.pop()
        return request if candidates else None
