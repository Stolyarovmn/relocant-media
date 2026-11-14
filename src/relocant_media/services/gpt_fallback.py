"""Ограниченный публичный запрос для подписочного GPT; без AI, сети и записи.

Внешний воркер проверяет хеш конверта, заново читает живые источники и только
после повторной валидации вызывает штатный import_package. Этот модуль не даёт
разрешение на публикацию и не ослабляет ограничения источников/категорий.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import AwareDatetime, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.ai.channel_style import writing_brief
from relocant_media.ai.models import PublicationStatus
from relocant_media.ai.package import (
    EditorialPackage,
    PackageEventDocument,
    StrictModel,
    canonical_json,
    evaluate_package,
)
from relocant_media.domain.city_profile import CityEditorialProfile, load_city_profile
from relocant_media.domain.dedup import event_key, find_repeated_publication
from relocant_media.domain.editorial_products import EditorialPresentation
from relocant_media.domain.freshness import evidence_freshness
from relocant_media.domain.source_locations import is_public_https_url
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services.editorial import (
    _candidate,
    _event_items,
    _reviewed_event_item,
    _snapshot_member,
)
from relocant_media.services.editorial_products import (
    build_presentation,
    recent_publications,
    verify_stored_presentation,
)
from relocant_media.services.native_map import bind_native_map_draft
from relocant_media.services.policy_reassessment import eligible_empty_policy_hold
from relocant_media.sources.registry import SourceConfig, load_source_registry
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

REQUEST_TTL = timedelta(hours=4)
MAX_REQUEST_BYTES = 512 * 1024
MAX_CANDIDATES = 5
MAX_DOCUMENTS = 8
MAX_DOCUMENT_BODY = 20_000
MAX_CANDIDATE_TEXT = 80_000
GEMINI_FAILURES = frozenset({"GeminiWorkerError", "GeminiCallTimeout", "GeminiQuotaCooldown"})
_TASK = "editorial-primary:belgrade"


class FallbackValidationError(ValueError):
    """Запрос или результат перестал соответствовать разрешённому резерву."""


class _CandidateRequest(StrictModel):
    raw_item_id: int = Field(gt=0)
    source_id: str = Field(min_length=1, max_length=200)
    article_id: str = Field(min_length=1, max_length=1000)
    event_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    documents: list[PackageEventDocument] = Field(min_length=1, max_length=MAX_DOCUMENTS)
    presentation_by_rubric: dict[str, EditorialPresentation]


class _FallbackRequest(StrictModel):
    version: Literal[1]
    scheduler_run_id: int = Field(gt=0)
    created_at: AwareDatetime
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    city_profile: CityEditorialProfile
    writing_brief: dict[str, Any] | None = None
    candidates: list[_CandidateRequest] = Field(min_length=1, max_length=MAX_CANDIDATES)


def _utc(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise FallbackValidationError("Нужно время с часовым поясом")
    return now.astimezone(UTC)


def _bounded_json(value: Any) -> str:
    text = canonical_json(value)
    if len(text.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise FallbackValidationError("Превышен размер резервного запроса")
    return text


def _failed_ids(run: SchedulerRun, now: datetime) -> list[int]:
    if (
        run.task_key != _TASK
        or run.task_type != "editorial_primary"
        or run.city != "belgrade"
        or run.status not in {"partial", "failed"}
        or run.finished_at is None
        or not timedelta(0) <= now - run.finished_at <= REQUEST_TTL
        or run.started_at > run.finished_at
    ):
        return []
    errors = run.details.get("processing_errors")
    if not isinstance(errors, list):
        return []
    result: list[int] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        raw_id = error.get("raw_item_id")
        if (
            isinstance(error.get("error_type"), str)
            and error["error_type"] in GEMINI_FAILURES
            and type(raw_id) is int
            and raw_id > 0
            and raw_id not in result
        ):
            result.append(raw_id)
    return result


def _public_host(url: str) -> str | None:
    # Проверяем HTTP по тем же правилам, что HTTPS; только стандартные порты.
    if any(character.isspace() or ord(character) < 32 or character == "\\" for character in url):
        return None
    try:
        parts = urlsplit(url)
        check = url
        if parts.scheme == "http":
            if (
                parts.port not in {None, 80}
                or parts.username is not None
                or parts.password is not None
            ):
                return None
            check = urlunsplit(
                ("https", parts.hostname or "", parts.path, parts.query, parts.fragment)
            )
        if not is_public_https_url(check):
            return None
        return (parts.hostname or "").removeprefix("www.")
    except ValueError:
        return None


def _registered_public(item: RawItem, source: Source, config: SourceConfig | None) -> bool:
    if config is None or not source.enabled or source.city != "belgrade":
        return False
    registry_host = _public_host(str(config.url))
    allowed = {
        registry_host,
        *(host.removeprefix("www.") for host in config.extraction.approved_hosts),
    }
    return bool(
        registry_host
        and _public_host(item.url) in allowed
        and _public_host(item.url) is not None
        and _public_host(source.url) == registry_host
        and source.source_type == config.source_type
        and source.ownership == config.ownership
        and source.high_risk == config.policy.high_risk
        and source.auto_publish == config.policy.auto_publish
    )


def _recent_runs(session: Session, now: datetime) -> list[SchedulerRun]:
    return list(session.scalars(
        select(SchedulerRun).where(
            SchedulerRun.task_key == _TASK,
            SchedulerRun.city == "belgrade",
            SchedulerRun.status.in_(("partial", "failed")),
            SchedulerRun.finished_at >= now - REQUEST_TTL,
            SchedulerRun.finished_at <= now,
        ).order_by(SchedulerRun.id.desc()).execution_options(populate_existing=True)
    ))


def _registry() -> dict[str, SourceConfig]:
    return {source.id: source for source in load_source_registry(Path("cities/belgrade/sources"))}


def pending_gpt_ids(session: Session, *, now: datetime) -> set[int]:
    """Give the subscription monitor a bounded first chance after a Gemini failure."""
    now = _utc(now)
    with session.no_autoflush:
        failed_ids = {
            raw_id for run in _recent_runs(session, now) for raw_id in _failed_ids(run, now)
        }
        if not failed_ids:
            return set()
        registry = _registry()
        unresolved = session.execute(select(RawItem, Source).join(Source).where(
            RawItem.id.in_(failed_ids),
        ))
        return {item.id for item, source in unresolved
                if _registered_public(item, source, registry.get(source.id))
                and (session.get(EditorialRun, item.id, populate_existing=True) is None
                     or eligible_empty_policy_hold(session, item.id, now=now))}


def _candidate_request(
    session: Session,
    raw_id: int,
    *,
    profile: CityEditorialProfile,
    registry: dict[str, SourceConfig],
    now: datetime,
) -> dict[str, Any]:
    item = session.get(RawItem, raw_id, populate_existing=True)
    source = session.get(Source, item.source_id, populate_existing=True) if item else None
    if (
        item is None
        or source is None
        or not _registered_public(item, source, registry.get(source.id))
    ):
        raise FallbackValidationError("Материал не относится к публичному реестру")
    members = _event_items(session, anchor=item, anchor_source=source)
    if not 1 <= len(members) <= MAX_DOCUMENTS:
        raise FallbackValidationError("Недопустимое число источников события")
    total = 0
    for member, policy in members:
        total += len(member.title) + len(member.body)
        if (
            not _registered_public(member, policy, registry.get(policy.id))
            or not 1 <= len(member.title) <= 1000
            or not 1 <= len(member.body) <= MAX_DOCUMENT_BODY
            or total > MAX_CANDIDATE_TEXT
            or evidence_freshness(
                member.title,
                member.body,
                member.published_at,
                now,
                precision=member.published_at_precision,
                source_type=policy.source_type,
                verified_at=member.ingested_at,
                timezone_name=profile.timezone,
            ).action
            != "keep"
        ):
            raise FallbackValidationError("Источник закрыт, устарел или превышает лимит")
    snapshots = [_snapshot_member(member, policy) for member, policy in members]
    documents = tuple(
        PackageEventDocument.model_validate_json(canonical_json(snapshot)) for snapshot in snapshots
    )
    first_rubric = profile.rubrics[0]
    presentation = build_presentation(
        session,
        item.id,
        documents,
        document_ids=[member.id for member, _ in members],
        profile=profile,
        rubric_id=first_rubric.id,
    )
    return {
        "raw_item_id": item.id,
        "source_id": source.id,
        "article_id": item.external_id,
        "event_key": event_key(_candidate(item)),
        "documents": snapshots,
        "presentation_by_rubric": {
            rubric.id: presentation.model_copy(
                update={
                    "rubric_id": rubric.id,
                    "rubric_name": rubric.name,
                }
            ).model_dump(mode="json")
            for rubric in profile.rubrics
        },
    }


def build_request(session: Session, *, now: datetime) -> dict[str, Any] | None:
    """Первое незавершённое окно с подтверждёнными сбоями Gemini за четыре часа."""
    now = _utc(now)
    with session.no_autoflush:
        profile = load_city_profile("belgrade")
        registry = _registry()
        for run in reversed(_recent_runs(session, now)):
            candidates: list[dict[str, Any]] = []
            request = {
                "version": 1,
                "scheduler_run_id": run.id,
                "created_at": now.isoformat(),
                "city_profile": profile.model_dump(mode="json"),
                "profile_sha256": profile.sha256,
                "writing_brief": writing_brief(profile, now),
                "candidates": candidates,
            }
            for raw_id in _failed_ids(run, now):
                if (session.get(EditorialRun, raw_id, populate_existing=True) is not None
                        and not eligible_empty_policy_hold(session, raw_id, now=now)):
                    continue
                try:
                    candidate = _candidate_request(
                        session, raw_id, profile=profile, registry=registry, now=now
                    )
                    raw = session.get(RawItem, raw_id)
                    source = session.get(Source, candidate["source_id"])
                    assert raw is not None and source is not None
                    if _reviewed_event_item(session, anchor=raw, anchor_source=source) is not None:
                        continue
                except ValueError:
                    continue
                candidates.append(candidate)
                try:
                    _FallbackRequest.model_validate_json(_bounded_json(request))
                except ValueError:
                    candidates.pop()
                if len(candidates) == MAX_CANDIDATES:
                    break
            if candidates:
                return request
    return None


def validate_fallback_package(
    session: Session,
    request: dict[str, Any],
    package: EditorialPackage,
    *,
    now: datetime,
) -> EditorialPackage:
    """Только проверка; живой fetch и штатный импорт выполняет вызывающий воркер."""
    now = _utc(now)
    if type(request.get("version")) is not int:
        raise FallbackValidationError("Некорректная версия запроса")
    parsed = _FallbackRequest.model_validate_json(_bounded_json(request))
    package = EditorialPackage.model_validate_json(_bounded_json(package.model_dump(mode="json")))
    ids = [candidate.raw_item_id for candidate in parsed.candidates]
    if len(ids) != len(set(ids)) or package.raw_item_id not in ids:
        raise FallbackValidationError("Чужой или повторяющийся кандидат")
    if not timedelta(0) <= now - parsed.created_at <= REQUEST_TTL:
        raise FallbackValidationError("Резервный запрос просрочен или датирован будущим")
    with session.no_autoflush:
        run = session.get(SchedulerRun, parsed.scheduler_run_id, populate_existing=True)
        if (
            run is None
            or package.raw_item_id not in _failed_ids(run, parsed.created_at)
            or run.finished_at is None
            or parsed.created_at < run.finished_at
        ):
            raise FallbackValidationError("Исходный сбой Gemini больше не подтверждён")
        profile = load_city_profile("belgrade")
        if parsed.profile_sha256 != profile.sha256 or request["city_profile"] != profile.model_dump(
            mode="json"
        ):
            raise FallbackValidationError("Профиль города изменился после запроса")
        if parsed.writing_brief is not None and parsed.writing_brief != writing_brief(
            profile, parsed.created_at,
        ):
            raise FallbackValidationError("Правила подачи не совпадают с резервным запросом")
        registry = _registry()
        expected = _candidate_request(
            session, package.raw_item_id, profile=profile, registry=registry, now=now
        )
        requested = next(
            candidate
            for candidate in parsed.candidates
            if candidate.raw_item_id == package.raw_item_id
        )
        if requested.model_dump(mode="json") != expected:
            raise FallbackValidationError("Снимок события или оформления изменился после запроса")
        if (
            package.version != 2
            or package.city != profile.city_id
            or package.event is None
            or package.source_id != expected["source_id"]
            or package.article_id != expected["article_id"]
            or package.event.event_key != expected["event_key"]
            or [doc.model_dump(mode="json") for doc in package.event.documents]
            != expected["documents"]
            or package.outcome.ranking.selection is None
            or package.presentation is None
        ):
            raise FallbackValidationError("Пакет не совпадает с точным событием запроса")
        rubric = package.outcome.ranking.selection.rubric_id
        if rubric not in requested.presentation_by_rubric:
            raise FallbackValidationError("Неизвестная рубрика резервного пакета")
        expected_presentation = bind_native_map_draft(
            requested.presentation_by_rubric[rubric],
            package.outcome.draft,
        )
        if package.presentation != expected_presentation:
            raise FallbackValidationError("Оформление пакета не соответствует запросу")
        verify_stored_presentation(
            session,
            package.raw_item_id,
            tuple(package.event.documents),
            document_ids=[doc.raw_item_id for doc in package.event.documents],
            presentation=package.presentation,
        )
        if any(
            review.verdict != "approve"
            or review.score < 75
            or review.flags
            or not review.live_source_checked
            or not parsed.created_at <= review.reviewed_at <= now
            or now - review.reviewed_at > timedelta(hours=2 if package.urgent else 24)
            for review in package.reviews
        ):
            raise FallbackValidationError("Нужны два новых независимых живых одобрения")
        outcome = evaluate_package(package, now=now)
        if package.outcome.status != PublicationStatus.READY or outcome.reason not in {
            "package_independently_reviewed",
            "package_requires_review",
        }:
            raise FallbackValidationError("Пакет не прошёл действующие редакционные проверки")
        existing = session.get(EditorialRun, package.raw_item_id, populate_existing=True)
        if existing is not None and not eligible_empty_policy_hold(
            session, package.raw_item_id, now=now,
        ):
            fingerprint = hashlib.sha256(
                canonical_json(package.model_dump(mode="json")).encode()
            ).hexdigest()
            if existing.outcome.get("_package_sha256") != fingerprint:
                raise FallbackValidationError("Материал уже обработан другим результатом")
            post = (
                session.get(Post, existing.post_id, populate_existing=True)
                if existing.post_id
                else None
            )
            if post is None or (post.title, post.body) != (
                package.outcome.draft.title,
                package.outcome.draft.body,
            ):
                raise FallbackValidationError("Сохранённый результат изменился")
        else:
            raw = session.get(RawItem, package.raw_item_id)
            source = session.get(Source, package.source_id)
            assert raw is not None and source is not None
            if _reviewed_event_item(session, anchor=raw, anchor_source=source) is not None:
                raise FallbackValidationError("Это событие уже обработано по другому материалу")
            history = recent_publications(session, package.city, now=now)
            if (
                find_repeated_publication(
                    package.outcome.draft.title,
                    package.outcome.draft.body,
                    [(row["id"], row["title"], row["body"]) for row in history],
                )
                is not None
            ):
                raise FallbackValidationError("Черновик повторяет уже подготовленную публикацию")
    return package
