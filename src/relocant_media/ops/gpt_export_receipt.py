"""Машинная квитанция экспорта после commit; сетевых действий здесь нет.

Это не новый GPT-протокол: приватный запрос остаётся существующим version=1.
Потребитель получает logs именно job.id из доверенного GitHub API, а не всего run.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

EXPORT_KIND = "committed_cycle_v1"
WORKFLOW = ".github/workflows/editorial-actions.yml"
REPOSITORY = "Stolyarovmn/relocant-media"
MAX_LOG_BYTES = 16*1024*1024
PUBLIC_SOURCE_TYPES = frozenset({
    "news_media", "official_venue_events", "official_utility_service_info", "official_guide",
    "official_heating_service_info", "official_transport_changes", "official_transport",
    "official_city_portal", "official_city_news", "official_municipal_news",
    "official_government_service", "official_health_service_info", "official_parking_service_news",
    "official_government_news", "official_rail_operator_news", "official_events",
    "official_government_guidance", "official_sports_news",
})
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_HEAD = re.compile(r"[0-9a-f]{40}\Z")
_GENERATION = re.compile(r"[0-9a-f]{32}\Z")
_KEYS = {"gpt_fallback_export", "gpt_fallback_request_sha256", "candidate_count",
         "scheduler_run_id", "snapshot_sha256", "generation", "workflow_run_id",
         "workflow_run_attempt", "head_sha", "job_key", "worker_mode"}


def _positive(value: Any, maximum: int = 2**63-1) -> bool:
    return type(value) is int and 1 <= value <= maximum


def validate_export_marker(value: Mapping[str, Any]) -> dict[str, Any]:
    if (not isinstance(value, Mapping) or set(value) != _KEYS
            or value.get("gpt_fallback_export") != EXPORT_KIND
            or value.get("job_key") != "cycle"
            or value.get("worker_mode") not in {"editorial", "drain"}
            or not _positive(value.get("candidate_count"), 5)
            or not _positive(value.get("scheduler_run_id"))
            or not _positive(value.get("workflow_run_id"))
            or not _positive(value.get("workflow_run_attempt"), 10000)):
        raise ValueError("Некорректная квитанция канонического экспорта")
    for field, pattern in (("gpt_fallback_request_sha256", _SHA), ("snapshot_sha256", _SHA),
                           ("head_sha", _HEAD), ("generation", _GENERATION)):
        text = value.get(field)
        if not isinstance(text, str) or pattern.fullmatch(text) is None:
            raise ValueError("Некорректная привязка канонического экспорта")
    return dict(value)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Повтор машинного поля квитанции")
        result[key] = value
    return result


def require_public_documents(request: Mapping[str, Any]) -> None:
    """Не отдавать закрытые документы внешнему reviewer даже с валидным receipt."""
    candidates = request.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 5:
        raise ValueError("Неверный публичный состав резервного запроса")
    references = request.get("published_post_refs", [])
    if not isinstance(references, list) or len(references) > 8:
        raise ValueError("Неверный публичный контекст прежних публикаций")
    from relocant_media.ai.linked_update import PublishedPostReference

    reference_ids: set[int] = set()
    message_ids: set[int] = set()
    for value in references:
        reference = PublishedPostReference.model_validate_json(json.dumps(value))
        if reference.raw_item_id in reference_ids or reference.message_id in message_ids:
            raise ValueError("Повтор ссылки на прежнюю публикацию")
        reference_ids.add(reference.raw_item_id)
        message_ids.add(reference.message_id)
        if reference.source_type not in PUBLIC_SOURCE_TYPES:
            raise ValueError("Закрытый тип прежней публикации")
    for candidate in candidates:
        documents = candidate.get("documents") if isinstance(candidate, Mapping) else None
        if not isinstance(documents, list) or not 1 <= len(documents) <= 8:
            raise ValueError("Нет ограниченного публичного набора документов")
        if ("flight_review_parent" in candidate
                and candidate["flight_review_parent"] not in references):
            raise ValueError("Исходная авиановость отсутствует в публичном контексте")
        for document in documents:
            if (not isinstance(document, Mapping)
                    or not isinstance(document.get("ownership"), str)
                    or document.get("ownership") not in {"official", "media"}
                    or type(document.get("high_risk")) is not bool
                    or type(document.get("auto_publish")) is not bool
                    or ("source_type" in document
                        and (not isinstance(document["source_type"], str)
                             or document["source_type"] not in PUBLIC_SOURCE_TYPES))):
                raise ValueError("Внешний экспорт допускает только публичные документы")


def validate_request_binding(marker: Mapping[str, Any], request: Mapping[str, Any]) -> None:
    """После проверки байтового SHA скачать существующий v1, не расширяя допуск.

    Это только привязка конверта к job receipt. Сроки/источники/пакет/ревью заново
    проверяет штатный импорт на текущем каноническом состоянии.
    """
    receipt = validate_export_marker(marker)
    require_public_documents(request)
    candidates = request.get("candidates")
    if (type(request.get("version")) is not int or request["version"] != 1
            or request.get("scheduler_run_id") != receipt["scheduler_run_id"]
            or type(request.get("scheduler_run_id")) is not int
            or request.get("batch_request_sha256") is not None
            or request.get("batch_result_sha256") is not None
            or not isinstance(candidates, list) or len(candidates) != receipt["candidate_count"]
            or any(not isinstance(candidate, Mapping)
                   or not _positive(candidate.get("raw_item_id")) for candidate in candidates)):
        raise ValueError("Приватный запрос не соответствует квитанции цикла")
    ids = [candidate["raw_item_id"] for candidate in candidates]
    content = json.dumps(request, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode()
    if (len(set(ids)) != len(ids) or len(content) > 512*1024
            or hashlib.sha256(content).hexdigest() != receipt["gpt_fallback_request_sha256"]):
        raise ValueError("Digest или состав приватного запроса не совпадает с квитанцией")


def marker_from_cycle_job(
    logs: bytes, *, run: Mapping[str, Any], job: Mapping[str, Any], request_sha256: str,
) -> dict[str, Any]:
    """Проверить успешный точный job/attempt, даже если последующий chain ещё работает.

    Logs должны быть скачаны по job.id из API именно этого run/attempt. Failed/cancelled
    run или job не получают исключения; для иных случаев нужен обычный fallback-preview.
    """
    repository = run.get("repository")
    status, conclusion = run.get("status"), run.get("conclusion")
    if (not isinstance(repository, Mapping) or repository.get("full_name") != REPOSITORY
            or run.get("path") != WORKFLOW or run.get("head_branch") != "main"
            or run.get("event") not in {"schedule", "workflow_dispatch"}
            or not ((status in {"in_progress", "queued", "pending", "waiting", "requested"}
                     and conclusion is None)
                    or (status == "completed" and conclusion == "success"))
            or job.get("name") != "cycle" or job.get("status") != "completed"
            or job.get("conclusion") != "success" or not _positive(job.get("id"))
            or not _positive(run.get("id")) or not _positive(run.get("run_attempt"))
            or not _positive(job.get("run_id")) or not _positive(job.get("run_attempt"))
            or job.get("run_id") != run.get("id")
            or job.get("run_attempt") != run.get("run_attempt")
            or job.get("head_sha") != run.get("head_sha")):
        raise ValueError("Не подтверждён успешный точный cycle job")
    if (not isinstance(request_sha256, str) or _SHA.fullmatch(request_sha256) is None
            or len(logs) > MAX_LOG_BYTES):
        raise ValueError("Некорректный digest или размер журнала")
    committed = False
    matches = []
    for line in logs.splitlines():
        start = line.find(b"{")  # GitHub добавляет timestamp перед JSON.
        if start < 0 or len(line)-start > 8192:
            continue
        try:
            value = json.loads(line[start:], object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("runtime_phase") == "state_commit_started":
            committed = False
        elif value.get("runtime_phase") == "state_committed":
            committed = True
        if value.get("gpt_fallback_export") != EXPORT_KIND:
            continue
        marker = validate_export_marker(value)
        if (not committed or marker["workflow_run_id"] != run["id"]
                or marker["workflow_run_attempt"] != run["run_attempt"]
                or marker["head_sha"] != run["head_sha"]):
            raise ValueError("Экспорт не привязан к зафиксированному cycle job")
        if marker["gpt_fallback_request_sha256"] == request_sha256:
            matches.append(marker)
    if len(matches) != 1:
        raise ValueError("Нет единственной подтверждённой квитанции этого digest")
    return matches[0]
