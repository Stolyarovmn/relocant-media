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
from relocant_media.ai.linked_update import PublishedPostReference
from relocant_media.ai.models import PublicationStatus
from relocant_media.ai.package import (
    EditorialPackage,
    PackageEventDocument,
    StrictModel,
    canonical_json,
    evaluate_package,
)
from relocant_media.domain.audience import local_utility_notice
from relocant_media.domain.city_profile import CityEditorialProfile, load_city_profile
from relocant_media.domain.dedup import event_key, find_repeated_publication
from relocant_media.domain.editorial_products import EditorialPresentation
from relocant_media.domain.freshness import evidence_freshness
from relocant_media.domain.policy import SOURCE_REPLACED_REASON
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
from relocant_media.services.flight_review import flight_review_marker, flight_review_reference
from relocant_media.services.material_lifecycle import (
    LifecycleBlockedError,
    can_retry_snapshot,
    ensure_not_terminal,
    fallback_reissued_at,
    is_terminal_blocked,
)
from relocant_media.services.native_map import bind_native_map_draft
from relocant_media.services.policy_reassessment import eligible_policy_reassessment
from relocant_media.sources.registry import SourceConfig, load_source_registry
from relocant_media.storage.models import EditorialRun, Post, RawItem, Source

REQUEST_TTL = timedelta(hours=4)
MAX_REQUEST_BYTES = 512 * 1024
MAX_CANDIDATES = 5
MAX_DOCUMENTS = 8
MAX_DOCUMENT_BODY = 20_000
MAX_CANDIDATE_TEXT = 80_000
GEMINI_FAILURES = frozenset({
    "GeminiWorkerError", "GeminiCallTimeout", "GeminiQuotaCooldown", "StructuredOutputError",
})
_RECOVERABLE_FAILURES = GEMINI_FAILURES | {"SourceUnavailableError"}
_TASK = "editorial-primary:belgrade"


class FallbackValidationError(ValueError):
    """Запрос или результат перестал соответствовать разрешённому резерву."""

    def __init__(self, message: str, *, code: str = "validation") -> None:
        super().__init__(message)
        # Фиксированный безопасный код причины для логов Actions.
        self.code = code


class _CandidateRequest(StrictModel):
    raw_item_id: int = Field(gt=0)
    source_id: str = Field(min_length=1, max_length=200)
    article_id: str = Field(min_length=1, max_length=1000)
    event_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    documents: list[PackageEventDocument] = Field(min_length=1, max_length=MAX_DOCUMENTS)
    presentation_by_rubric: dict[str, EditorialPresentation]
    flight_review_parent: PublishedPostReference | None = None


class _FallbackRequest(StrictModel):
    version: Literal[1]
    scheduler_run_id: int = Field(gt=0)
    created_at: AwareDatetime
    profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    city_profile: CityEditorialProfile
    writing_brief: dict[str, Any] | None = None
    batch_request_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    batch_result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    candidates: list[_CandidateRequest] = Field(min_length=1, max_length=MAX_CANDIDATES)
    published_post_refs: list[PublishedPostReference] = Field(default_factory=list, max_length=8)


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
    result: list[int] = []
    for key in ("flight_review_ids", "lifecycle_fallback_ids"):
        fallback_ids = run.details.get(key)
        if (isinstance(fallback_ids, list) and len(fallback_ids) <= MAX_CANDIDATES
                and all(type(raw_id) is int and raw_id > 0 for raw_id in fallback_ids)
                and len(set(fallback_ids)) == len(fallback_ids)):
            result.extend(raw_id for raw_id in fallback_ids if raw_id not in result)
    for error in errors if isinstance(errors, list) else []:
        if not isinstance(error, dict):
            continue
        raw_id = error.get("raw_item_id")
        if (
            isinstance(error.get("error_type"), str)
            and error["error_type"] in _RECOVERABLE_FAILURES
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


def refresh_target(session: Session, run: SchedulerRun, original_id: int) -> int:
    """Серверная квитанция разрешает другой ID только в исходном задании."""
    receipts = run.details.get("gpt_evidence_refresh", {})
    if not isinstance(receipts, dict):
        raise FallbackValidationError("Некорректная квитанция пересъёма")
    receipt = receipts.get(str(original_id))
    if receipt is None:
        return original_id
    if not isinstance(receipt, dict):
        raise FallbackValidationError("Некорректная квитанция пересъёма")
    raw_id = receipt.get("replacement_raw_item_id")
    if raw_id is None:
        return original_id
    marker = session.get(EditorialRun, original_id, populate_existing=True)
    if (type(raw_id) is not int or not 0 < raw_id < 2**63 or raw_id == original_id
            or marker is None or marker.post_id is not None
            or marker.outcome.get("reason") != SOURCE_REPLACED_REASON
            or marker.outcome.get("replacement_raw_item_id") != raw_id):
        raise FallbackValidationError("Нет связанной серверной записи о замене источника")
    return raw_id


def pending_gpt_ids(session: Session, *, now: datetime) -> set[int]:
    """Give the subscription monitor a bounded first chance after a Gemini failure."""
    now = _utc(now)
    from relocant_media.services.gpt_batch_fallback import pending_batch_ids

    batch_ids = pending_batch_ids(session, now=now)
    with session.no_autoflush:
        failed_ids = {
            refresh_target(session, run, raw_id)
            for run in _recent_runs(session, now) for raw_id in _failed_ids(run, now)
        }
        if not failed_ids:
            return batch_ids
        registry = _registry()
        unresolved = session.execute(select(RawItem, Source).join(Source).where(
            RawItem.id.in_(failed_ids),
        ))
        return batch_ids | {item.id for item, source in unresolved
                if _registered_public(item, source, registry.get(source.id))
                and not is_terminal_blocked(session, item.id, now=now)
                and (session.get(EditorialRun, item.id, populate_existing=True) is None
                     or flight_review_marker(session.get(EditorialRun, item.id)) is not None
                     or eligible_policy_reassessment(session, item.id, now=now)
                     or can_retry_snapshot(session, item.id, now=now))}


def _ensure_fallback_open(session: Session, raw_id: int, *, now: datetime) -> None:
    # Старую отправку можно только сверить; import_package отдельно требует точный хеш.
    existing = session.get(EditorialRun, raw_id, populate_existing=True)
    if existing is not None and existing.delivery_status == "sent":
        return
    try:
        ensure_not_terminal(session, raw_id, now=now)
    except LifecycleBlockedError as error:
        raise FallbackValidationError(str(error), code="material_closed") from error


def _candidate_request(
    session: Session,
    raw_id: int,
    *,
    profile: CityEditorialProfile,
    registry: dict[str, SourceConfig],
    now: datetime,
) -> dict[str, Any]:
    _ensure_fallback_open(session, raw_id, now=now)
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
            or (evidence_freshness(
                member.title,
                member.body,
                member.published_at,
                now,
                precision=member.published_at_precision,
                source_type=policy.source_type,
                verified_at=member.ingested_at,
                timezone_name=profile.timezone,
            ).action != "keep")
        ):
            raise FallbackValidationError("Источник закрыт, устарел или превышает лимит")
    snapshots = [_snapshot_member(member, policy) for member, policy in members]
    documents = tuple(
        PackageEventDocument.model_validate_json(canonical_json(snapshot)) for snapshot in snapshots
    )
    request = {
        "raw_item_id": item.id,
        "source_id": source.id,
        "article_id": item.external_id,
        "event_key": event_key(_candidate(item)),
        "documents": snapshots,
        "presentation_by_rubric": {
            rubric.id: build_presentation(
                session, item.id, documents,
                document_ids=[member.id for member, _ in members],
                profile=profile, rubric_id=rubric.id,
            ).model_dump(mode="json")
            for rubric in profile.rubrics
        },
    }
    parent = flight_review_reference(
        session, session.get(EditorialRun, raw_id), city=profile.city_id, now=now,
    )
    if parent is not None:
        if any(doc.ownership not in {"official", "media"} or doc.high_risk for doc in documents):
            raise FallbackValidationError("Связанная проверка допускает публичные документы")
        request["flight_review_parent"] = parent.model_dump(mode="json")
    return request


def _apply_request_references(request: dict[str, Any], references: list[dict[str, Any]]) -> None:
    parents = [candidate["flight_review_parent"] for candidate in request["candidates"]
               if "flight_review_parent" in candidate]
    unique: dict[int, dict[str, Any]] = {}
    for reference in parents + references:
        unique.setdefault(reference["message_id"], reference)
    if unique:
        request["published_post_refs"] = list(unique.values())[:8]
    else:
        request.pop("published_post_refs", None)


def build_request(
    session: Session, *, now: datetime, scheduler_run_id: int | None = None,
) -> dict[str, Any] | None:
    """Действующие коммунальные предупреждения идут перед обычной очередью резерва."""
    now = _utc(now)
    with session.no_autoflush:
        profile = load_city_profile("belgrade")
        registry = _registry()
        windows = [(run, list(dict.fromkeys(refresh_target(session, run, raw_id)
                    for raw_id in _failed_ids(run, now)))) for run in _recent_runs(session, now)
                   if scheduler_run_id is None or run.id == scheduler_run_id]
        pending = pending_gpt_ids(session, now=now)
        pending_items = session.execute(
            select(RawItem, Source).join(Source).where(RawItem.id.in_(pending)),
        ).all()
        official = {item.id for item, source in pending_items if source.ownership == "official"}
        flights = {item.id for item, _ in pending_items
                   if flight_review_marker(session.get(EditorialRun, item.id)) is not None}
        urgent = {
            item.id for item, source in pending_items
            if local_utility_notice(item.source_id, item.url, item.title, item.body)
            and evidence_freshness(
                item.title, item.body, item.published_at, now,
                precision=item.published_at_precision, source_type=source.source_type,
                verified_at=item.ingested_at, timezone_name=profile.timezone,
            ).action == "keep"
        }
        # В пределах одного приоритета сначала разбираем более ранний сбой:
        # иначе каждый новый цикл может вытеснять открытые задания до истечения срока.
        windows.sort(key=lambda window: (
            urgent.isdisjoint(window[1]), flights.isdisjoint(window[1]),
            official.isdisjoint(window[1]), window[0].id,
        ))
        from relocant_media.services.linked_updates import published_post_references

        references = published_post_references(session, city=profile.city_id, now=now)
        for run, failed in windows:
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
            for raw_id in sorted(failed, key=lambda raw_id: (
                raw_id not in urgent, raw_id not in flights, raw_id not in official,
            )):
                existing = session.get(EditorialRun, raw_id, populate_existing=True)
                if (existing is not None and flight_review_marker(existing) is None
                        and not eligible_policy_reassessment(session, raw_id, now=now)
                        and not can_retry_snapshot(session, raw_id, now=now)):
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
                _apply_request_references(request, references)
                try:
                    _FallbackRequest.model_validate_json(_bounded_json(request))
                except ValueError:
                    candidates.pop()
                    _apply_request_references(request, references)
                if len(candidates) == MAX_CANDIDATES:
                    break
            if candidates:
                return request
    return None


def validate_fallback_candidate(
    session: Session,
    request: dict[str, Any],
    raw_id: int,
    *,
    now: datetime,
) -> tuple[_FallbackRequest, CityEditorialProfile, _CandidateRequest, dict[str, Any]]:
    """Границы сбоя и доказательства одинаковы для готового пакета и отказа."""
    now = _utc(now)
    if type(request.get("version")) is not int:
        raise FallbackValidationError("Некорректная версия запроса")
    _ensure_fallback_open(session, raw_id, now=now)
    parsed = _FallbackRequest.model_validate_json(_bounded_json(request))
    ids = [candidate.raw_item_id for candidate in parsed.candidates]
    if len(ids) != len(set(ids)) or raw_id not in ids:
        raise FallbackValidationError("Чужой или повторяющийся кандидат")
    if not timedelta(0) <= now - parsed.created_at <= REQUEST_TTL:
        raise FallbackValidationError(
            "Резервный запрос просрочен или датирован будущим", code="request_expired",
        )
    reissued = fallback_reissued_at(session, raw_id)
    if reissued is not None and parsed.created_at < reissued:
        raise FallbackValidationError(
            "Резервный запрос выпущен раньше нового окна", code="request_expired",
        )
    with session.no_autoflush:
        run = session.get(SchedulerRun, parsed.scheduler_run_id, populate_existing=True)
        batch = run.details.get("batch_preselection") if run is not None else None
        failed_ids = set(_failed_ids(run, parsed.created_at)) if run is not None else set()
        selected_ids = set(batch.get("gpt_selected_ids", ())) if isinstance(batch, dict) else set()
        from_batch = (
            parsed.batch_request_sha256 is not None or parsed.batch_result_sha256 is not None
        )
        if from_batch:
            from relocant_media.services.gpt_batch_fallback import validate_batch_handoff

            validate_batch_handoff(run, now=now)
        origins = failed_ids | selected_ids if from_batch else failed_ids
        pairs = ([(origin, refresh_target(session, run, origin))
                  for origin in origins] if run is not None else [])
        targets = {target: origin for origin, target in pairs if target == origin}
        # Уже выбранный официальный документ может оказаться целью замены.
        # В этом случае приоритет у привязки и хеша пересъёма.
        targets.update((target, origin) for origin, target in pairs if target != origin)
        ranked_scope = (
            isinstance(batch, dict) and batch.get("status") == "gpt_ranked"
            and parsed.batch_request_sha256 is not None and parsed.batch_result_sha256 is not None
            and parsed.batch_request_sha256 == batch.get("gpt_request_sha256")
            and parsed.batch_result_sha256 == batch.get("gpt_rank_sha256")
            and set(ids) <= targets.keys()
        )
        if (
            run is None
            or (from_batch and not ranked_scope)
            or raw_id not in targets
            or run.finished_at is None
            or parsed.created_at < run.finished_at
            or (from_batch and not timedelta(0) <= now - run.finished_at <= REQUEST_TTL)
        ):
            raise FallbackValidationError(
                "Исходный сбой Gemini больше не подтверждён", code="primary_failure_unconfirmed",
            )
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
            session, raw_id, profile=profile, registry=registry, now=now
        )
        origin = targets[raw_id]
        if from_batch and origin in selected_ids:
            from relocant_media.services.gpt_refresh import refreshed_anchor_sha256

            assert isinstance(batch, dict)
            anchor = next(doc for doc in expected["documents"]
                          if doc["raw_item_id"] == raw_id)
            hashes = batch.get("gpt_evidence_sha256")
            if not isinstance(hashes, dict) or (
                refreshed_anchor_sha256(run, origin) or hashes.get(str(origin))
            ) != hashlib.sha256(canonical_json(anchor).encode()).hexdigest():
                raise FallbackValidationError("Исходник изменился после пакетного отбора GPT")
        requested = next(
            candidate
            for candidate in parsed.candidates
            if candidate.raw_item_id == raw_id
        )
        if requested.flight_review_parent is not None and "flight_review_parent" not in expected:
            existing = session.get(EditorialRun, raw_id, populate_existing=True)
            if existing is not None:
                archived = existing.outcome.get("_previous_policy_hold", {}).get("outcome", {})
                link = existing.outcome.get("_package", {}).get("linked_update", {})
                marker = archived.get("_flight_review")
                if marker == {"raw_item_id": requested.flight_review_parent.raw_item_id,
                              "message_id": requested.flight_review_parent.message_id}:
                    expected["flight_review_parent"] = link.get("parent")
        if requested.model_dump(mode="json", exclude={"flight_review_parent"}
                                if requested.flight_review_parent is None else None) != expected:
            raise FallbackValidationError(
                "Снимок события или оформления изменился после запроса", code="snapshot_changed",
            )
        if (requested.flight_review_parent is not None
                and requested.flight_review_parent not in parsed.published_post_refs):
            raise FallbackValidationError("Исходная авиановость отсутствует в запросе")
        return parsed, profile, requested, expected


def validate_fallback_package(
    session: Session,
    request: dict[str, Any],
    package: EditorialPackage,
    *,
    now: datetime,
) -> EditorialPackage:
    """Только проверка; живой fetch и штатный импорт выполняет вызывающий воркер."""
    package = EditorialPackage.model_validate_json(_bounded_json(package.model_dump(mode="json")))
    parsed, profile, requested, expected = validate_fallback_candidate(
        session, request, package.raw_item_id, now=now,
    )
    with session.no_autoflush:
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
        if requested.flight_review_parent is not None and (
            package.linked_update is None
            or package.linked_update.parent != requested.flight_review_parent
        ):
            raise FallbackValidationError(
                "Нужна проверенная связь с исходной авиановостью", code="flight_parent_mismatch",
            )
        if package.linked_update is not None:
            from relocant_media.services.linked_updates import validate_linked_update

            if package.linked_update.parent not in parsed.published_post_refs:
                raise FallbackValidationError("Связь отсутствует в каноническом запросе")
            try:
                validate_linked_update(session, package, now=now)
            except ValueError as error:
                raise FallbackValidationError(
                    "Изменился контекст связанной публикации", code="linked_context_changed",
                ) from error
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
            raise FallbackValidationError(
                "Нужны два новых независимых живых одобрения", code="reviews_not_fresh",
            )
        outcome = evaluate_package(package, now=now)
        if package.outcome.status != PublicationStatus.READY or outcome.reason not in {
            "package_independently_reviewed",
            "package_requires_review",
            "event_place_requires_verification",
        }:
            raise FallbackValidationError("Пакет не прошёл действующие редакционные проверки")
        existing = session.get(EditorialRun, package.raw_item_id, populate_existing=True)
        if (existing is not None and flight_review_marker(existing) is None
                and not eligible_policy_reassessment(session, package.raw_item_id, now=now)
                and not can_retry_snapshot(session, package.raw_item_id, now=now)):
            fingerprint = hashlib.sha256(
                canonical_json(package.model_dump(mode="json")).encode()
            ).hexdigest()
            if existing.outcome.get("_package_sha256") != fingerprint:
                raise FallbackValidationError(
                    "Материал уже обработан другим результатом", code="already_processed",
                )
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
