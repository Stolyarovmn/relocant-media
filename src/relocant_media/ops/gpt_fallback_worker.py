"""Проверить готовые GPT-пакеты и передать их штатной доставке без новых вызовов ИИ."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from relocant_media.ai.models import (
    EditorialOutcome,
    EvidenceDocument,
    PublicationStatus,
    RankingResult,
)
from relocant_media.ai.package import EditorialPackage, canonical_json
from relocant_media.ops import server_cycle, server_runtime
from relocant_media.services.editorial import EditorialService
from relocant_media.services.gpt_batch_fallback import validate_batch_rank
from relocant_media.services.gpt_fallback import (
    validate_fallback_candidate,
    validate_fallback_package,
)
from relocant_media.services.gpt_refresh import RefreshInput, refresh_gpt_evidence
from relocant_media.services.package_import import import_package
from relocant_media.services.policy_reassessment import (
    eligible_empty_policy_hold,
    eligible_policy_reassessment,
    persist_policy_reassessment,
)
from relocant_media.storage.models import EditorialRun
from relocant_media.storage.sqlite_schema import verify_sqlite_schema

MAX_INPUT_BYTES = 16 * 1024 * 1024


class FallbackRejection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", str_strip_whitespace=True)
    raw_item_id: int = Field(gt=0, lt=2**63)
    category: str = Field(min_length=1, max_length=40)
    rationale: str = Field(min_length=1, max_length=800)


class FallbackResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    version: Literal[1]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    packages: tuple[EditorialPackage, ...] = Field(max_length=5)
    rejected: tuple[FallbackRejection, ...] = Field(default=(), max_length=5)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Версия результата должна быть целым числом")
        return value

    @model_validator(mode="after")
    def unique_packages(self) -> Self:
        ids = [package.raw_item_id for package in self.packages]
        ids.extend(item.raw_item_id for item in self.rejected)
        if not 1 <= len(ids) <= 5 or len(ids) != len(set(ids)):
            raise ValueError("Ожидаются от одного до пяти уникальных решений")
        return self


class FallbackInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    request: dict[str, Any]
    result: FallbackResult

    @model_validator(mode="after")
    def bound_request(self) -> Self:
        expected = hashlib.sha256(canonical_json(self.request).encode("utf-8")).hexdigest()
        if self.result.request_sha256 != expected:
            raise ValueError("Ответ не соответствует исходному запросу")
        return self


def _error_type(error: Exception) -> str:
    name = type(error).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", name) else "Error"


def save_batch_rank(
    engine: Engine, request: dict[str, Any], result: dict[str, Any], *, now: datetime,
) -> dict[str, Any]:
    """Сохранить одну принятую выборку на цикл в каноническом воркере Actions."""
    with Session(engine) as session:
        run, ids = validate_batch_rank(session, request, result, now=now)
        batch = run.details["batch_preselection"]
        outstanding = bool(ids or run.details.get("processing_errors"))
        if batch["status"] == "gpt_waiting":
            run.details = {**run.details, "batch_preselection": {
                **batch,
                "status": "gpt_ranked",
                "gpt_selected_ids": ids,
                "gpt_evidence_sha256": {
                    str(item["raw_item_id"]): item["evidence_sha256"]
                    for item in request["candidates"] if item["raw_item_id"] in ids
                },
                "gpt_rank_sha256": hashlib.sha256(canonical_json(result).encode()).hexdigest(),
                "gpt_request_sha256": result["request_sha256"],
            }}
            session.commit()
    return {
        "status": "PRIMARY_PARTIAL" if outstanding else "PRIMARY_OK",
        "mode": "subscription_gpt_batch_rank",
        "selected_count": len(ids),
        "fallback_required": outstanding,
        "model_calls": 0,
    }


def _save_rejection(
    engine: Engine, request: dict[str, Any], rejection: FallbackRejection, *, now: datetime,
) -> None:
    with Session(engine) as session:
        raw_id = rejection.raw_item_id
        _, profile, candidate, _ = validate_fallback_candidate(session, request, raw_id, now=now)
        if rejection.category not in {category for rubric in profile.rubrics
                                      for category in rubric.categories}:
            raise ValueError("Неизвестная категория отказа GPT")
        documents = tuple(EvidenceDocument.model_validate(
            doc.model_dump(include=set(EvidenceDocument.model_fields)))
            for doc in candidate.documents)
        ranking = RankingResult(
            should_process=False, relevance=0, category=rejection.category,
            confidence=0, rationale=rejection.rationale,
        )
        outcome = EditorialOutcome(
            status=PublicationStatus.SKIPPED, reason="gpt_ranking_rejected",
            ranking=ranking, confidence=0,
        )
        existing = session.get(EditorialRun, raw_id, populate_existing=True)
        if existing is None:
            EditorialService(session, server_cycle._CachedOutcome(
                outcome, documents, profile.city_id,
            )).prepare(raw_id)
        elif eligible_empty_policy_hold(session, raw_id, now=now):
            persist_policy_reassessment(session, raw_id, outcome, documents, now=now)
        elif (existing.delivery_status != "pending" or existing.post_id is not None
              or existing.staging_message_id is not None
              or any(existing.outcome.get(key) != value
                     for key, value in outcome.model_dump(mode="json").items())):
            raise ValueError("Материал уже обработан другим решением")


def _check_live_evidence(package: EditorialPackage) -> None:
    if package.event is None:
        raise ValueError("GPT-пакет должен содержать полный комплект источников")
    presentation = package.presentation
    if presentation is not None:
        government_urls = {document.url for document in package.event.documents
                           if document.source_id == server_runtime.SOURCE_ID}
        source_map = presentation.source_map or {}
        point = (presentation.native_map or {}).get("point", {})
        if (source_map.get("source_page") in government_urls
                or isinstance(point, dict) and point.get("source_url") in government_urls):
            # У этого узкого адаптера пока нет HTML-проверки карты. Текстовый
            # материал проходит обычный fetch, карта требует полного адаптера.
            raise ValueError("Адаптер источника не поддерживает живую проверку карты")
    # Штатный адаптер проверяет не только текст, но и карты в живом HTML.
    live = server_runtime.fetch_documents(package)
    expected = package.event.documents
    if len(live) != len(expected) or any(
        actual.model_dump() != saved.model_dump(include=set(EvidenceDocument.model_fields))
        for actual, saved in zip(live, expected, strict=True)
    ):
        raise ValueError("Живые источники изменились после подготовки GPT-пакета")


async def _publish_imported(
    engine: Engine, raw_ids: set[int], blocked: list[dict[str, Any]],
) -> list[int]:
    # Применяем тот же расширенный live-fetch, что и основной server_runtime;
    # восстанавливаем модульный адаптер даже при неясном результате отправки.
    previous_fetch = server_cycle.fetch_documents
    server_cycle.fetch_documents = server_runtime.fetch_documents
    try:
        return await server_cycle.publish_pending(
            engine, enabled=True, only_raw_ids=raw_ids, blocked=blocked,
        )
    finally:
        server_cycle.fetch_documents = previous_fetch


def run_worker(
    engine: Engine, payload: FallbackInput, *, send_enabled: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Каждый импорт отдельно проходит актуальные проверки; ошибки не раскрывают пакет."""
    if now is not None and now.tzinfo is None:
        raise ValueError("Время проверки должно содержать часовой пояс")
    imported = 0
    rejected = 0
    ready = 0
    declined = 0
    delivery_ids: set[int] = set()
    errors: list[dict[str, str]] = []
    for rejection in payload.result.rejected:
        try:
            _save_rejection(engine, payload.request, rejection, now=now or datetime.now(UTC))
            declined += 1
        except Exception as error:
            rejected += 1
            errors.append({"stage": "reject", "error_type": _error_type(error)})
    for package in payload.result.packages:
        phase = "validate"
        try:
            with Session(engine) as session:
                package = validate_fallback_package(
                    session, payload.request, package, now=now or datetime.now(UTC),
                )
            phase = "live"
            _check_live_evidence(package)
            # Сетевое чтение не держит транзакцию. После него ещё раз проверяем
            # актуальные исходники, решения и привязку к зафиксированной ошибке Gemini.
            phase = "revalidate"
            with Session(engine) as session:
                package = validate_fallback_package(
                    session, payload.request, package, now=now or datetime.now(UTC),
                )
                phase = "import"
                run = import_package(session, package, now=now, replace_empty_hold=True,
                                     replace_map_hold=True)
                imported += 1
                if run.outcome.get("status") == "ready":
                    ready += 1
                    if run.delivery_status == "pending":
                        delivery_ids.add(package.raw_item_id)
        except Exception as error:
            rejected += 1
            errors.append({"stage": phase, "error_type": _error_type(error)})
    blocked: list[dict[str, Any]] = []
    sent_count = 0
    if send_enabled is True and delivery_ids:
        try:
            sent_count = len(asyncio.run(_publish_imported(engine, delivery_ids, blocked)))
        except Exception as error:
            errors.append({"stage": "publish", "error_type": _error_type(error)})
    candidates = payload.request.get("candidates", [])
    requested_ids = ({candidate["raw_item_id"] for candidate in candidates[:5]
                      if isinstance(candidate, dict)
                      and type(candidate.get("raw_item_id")) is int
                      and 0 < candidate["raw_item_id"] < 2**63}
                     if isinstance(candidates, list) else set())
    with Session(engine) as session:
        saved_ids = session.scalars(select(EditorialRun.raw_item_id).where(
            EditorialRun.raw_item_id.in_(requested_ids),
            EditorialRun.outcome["status"].as_string().in_({
                "ready", "human_review", "skipped", "blocked",
            }),
        )).all()
        resolved = {raw_id for raw_id in saved_ids if not eligible_policy_reassessment(
            session, raw_id, now=now,
        )}
    unresolved = len(requested_ids - resolved)
    status = ("PRIMARY_FAILED" if not (imported or declined) else
              "PRIMARY_PARTIAL" if rejected or errors or blocked or unresolved else "PRIMARY_OK")
    return {
        "status": status, "mode": "subscription_gpt_fallback",
        "received": len(payload.result.packages), "imported": imported, "ready": ready,
        "rejected": rejected, "declined": declined, "sent_count": sent_count,
        "delivery_blocked": len(blocked),
        "delivery_enabled": send_enabled is True, "processing_errors": errors,
        "unresolved": unresolved,
        "fallback_required": bool(unresolved or errors or blocked),
        "model_calls": 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    engine = None
    output_created = False
    try:
        args.output.mkdir(parents=True, exist_ok=False)
        output_created = True
        if not args.input.is_file() or args.input.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError("Приватный входной файл отсутствует или слишком велик")
        exchange = args.input.read_text(encoding="utf-8")
        value = json.loads(exchange)
        result = value.get("result") if isinstance(value, dict) else None
        kind = result.get("kind") if isinstance(result, dict) else None
        rank_only = kind == "batch_preselection"
        refresh = RefreshInput.model_validate_json(exchange) if kind == "evidence_refresh" else None
        payload = None if rank_only or refresh is not None else FallbackInput.model_validate_json(
            exchange,
        )
        url = make_url(os.environ["DATABASE_URL"])
        if (url.get_backend_name() != "sqlite" or not url.database or url.query
                or not Path(url.database).is_file()):
            raise ValueError("Требуется существующая локальная SQLite")
        engine = create_engine(url)
        verify_sqlite_schema(engine)
        if refresh is not None:
            request = refresh_gpt_evidence(engine, refresh)
            (args.output / "gpt-refreshed-request.json").write_text(
                canonical_json(request), encoding="utf-8",
            )
            report = {
                "status": "PRIMARY_PARTIAL", "mode": "subscription_gpt_evidence_refresh",
                "fallback_required": True, "model_calls": 0,
                "sources": len(request["candidates"][0]["documents"]),
            }
        elif payload is None:
            if set(value) != {"request", "result"} or not isinstance(value["request"], dict):
                raise ValueError("Ожидался приватный запрос и результат отбора")
            report = save_batch_rank(
                engine, value["request"], value["result"], now=datetime.now(UTC),
            )
        else:
            report = run_worker(
                engine, payload, send_enabled=os.environ.get("EDITORIAL_SEND_ENABLED") == "true",
            )
    except Exception as error:
        report = {"status": "PRIMARY_FAILED", "mode": "subscription_gpt_fallback",
                  "error_type": _error_type(error), "model_calls": 0,
                  "fallback_required": True}
    finally:
        if engine is not None:
            engine.dispose()
    if output_created:
        (args.output / "server-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] not in {"PRIMARY_OK", "PRIMARY_PARTIAL"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
