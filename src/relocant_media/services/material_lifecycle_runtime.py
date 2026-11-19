"""Транзакционные границы конечного жизненного цикла для обычных workers.

Контроллер не публикует и не создаёт редакционный пакет. Delegate обязан пройти
обычный полный pipeline и все независимые проверки допуска.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument, RankingResult
from relocant_media.ai.provider import StructuredAIProvider
from relocant_media.ai.rejection_review import audit_rejection
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.scheduler.models import SchedulerRun
from relocant_media.services import material_lifecycle as core
from relocant_media.services.editorial import source_replacement_target
from relocant_media.services.policy_reassessment import eligible_policy_reassessment
from relocant_media.storage.models import EditorialRun, RawItem, Source


class _FreshRankProvider:
    def __init__(self, provider: StructuredAIProvider, context_sha256: str):
        self.provider, self.context_sha256 = provider, context_sha256

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        if task == "rank":
            # Wrapper находится снаружи exact cache: старый ошибочный rank не совпадёт.
            system_instruction += (
                "\nПолная повторная оценка после отдельной проверки прежнего отказа. "
                "Не наследуй прежнее решение; заново проверь исходные документы, "
                "профиль и все требования отбора. Это не разрешение публикации. "
                f"rejection_review_context_sha256={self.context_sha256}"
            )
        return self.provider.generate_json(task=task, system_instruction=system_instruction,
            prompt=prompt, response_schema=response_schema)


class MaterialLifecycleRuntime:
    def __init__(self, engine: Engine, city: str, provider: StructuredAIProvider,
                 now: datetime | None = None):
        if now is not None and (now.tzinfo is None or now.utcoffset() is None):
            raise ValueError("Нужно время с часовым поясом")
        self.engine, self.city, self.provider, self.now = engine, city, provider, now

    def _time(self) -> datetime:
        return self.now.astimezone(UTC) if self.now is not None else datetime.now(UTC)

    def _state(self, session: Session, raw_id: int) -> dict[str, Any]:
        raw = session.get(RawItem, raw_id)
        source = session.get(Source, raw.source_id) if raw else None
        if source is None or source.city != self.city:
            raise ValueError("Материал не принадлежит городу контроллера")
        return core.lifecycle_state(session, raw_id, now=self._time())

    def _record(self, session: Session, state: dict[str, Any]) -> SchedulerRun:
        # Core возвращает root ledger и для заменённого источника.
        record = session.scalar(select(SchedulerRun).where(
            SchedulerRun.task_type == core.TASK_TYPE,
            SchedulerRun.task_key == f"material-lifecycle:{self.city}:{state['raw_item_id']}",
        ).order_by(SchedulerRun.id.desc()).limit(1))
        if record is None:
            raise ValueError("Отсутствует закрытый журнал жизненного цикла")
        return record

    def _target(self, session: Session, raw_id: int) -> int:
        replacement_id = source_replacement_target(session, raw_id)
        if replacement_id is None:
            return raw_id
        core.inherit_lifecycle(session, raw_id, replacement_id, now=self._time())
        return replacement_id

    def reconcile(self) -> core.LifecycleSweep:
        with Session(self.engine) as session:
            sweep = core.sweep_materials(session, city=self.city, now=self._time())
            records = {row.details.get("raw_item_id"): row.details
                       for row in session.scalars(select(SchedulerRun).where(
                           SchedulerRun.city == self.city,
                           SchedulerRun.task_type == core.TASK_TYPE,
                       ).order_by(SchedulerRun.id))}
            due = []
            for raw_id in sweep.due_ids:
                state = records[raw_id]
                if "lifecycle_root_raw_id" in state:
                    state = records[state["lifecycle_root_raw_id"]]
                # Новые материалы выбирает обычный selector, сохраняя его guards.
                if not (state["next_action"] == "prepare" and state["attempts"] == 0):
                    due.append(raw_id)
            session.commit()
        return core.LifecycleSweep(tuple(due), sweep.rejection_audit_ids, sweep.fallback_ids,
                                   sweep.excluded_ids, sweep.blocked_ids)

    def provider_for(self, raw_id: int) -> StructuredAIProvider:
        """Delegate берёт этот provider снаружи PublicPreparationCache."""
        with Session(self.engine) as session:
            state = self._state(session, raw_id)
            review = self._record(session, state).details.get("rejection_review", {})
            session.commit()
        if (isinstance(review, dict) and review.get("decision") == "retry_processing"
                and isinstance(review.get("context_sha256"), str)):
            return _FreshRankProvider(self.provider, review["context_sha256"])
        return self.provider

    def _documents(self, session: Session, raw_id: int, run: EditorialRun
                   ) -> tuple[EvidenceDocument, ...]:
        snapshots = run.evidence.get("documents")
        if snapshots is None:
            snapshots = [{**run.evidence, "raw_item_id": raw_id}]
        if not isinstance(snapshots, list) or not snapshots:
            raise ValueError("Отсутствуют исходные документы отказа")
        result = []
        anchor_found = False
        for snapshot in snapshots:
            if not isinstance(snapshot, dict) or type(snapshot.get("raw_item_id")) is not int:
                raise ValueError("Неверный идентификатор исходного документа")
            item = session.get(RawItem, snapshot["raw_item_id"])
            source = session.get(Source, item.source_id) if item else None
            if item is None or source is None or not source.enabled or source.city != self.city:
                raise ValueError("Источник доказательств отсутствует или отключён")
            document = EvidenceDocument.model_validate(snapshot)
            current = EvidenceDocument(source_id=item.source_id, url=item.url, title=item.title,
                body=item.body, published_at=item.published_at,
                published_at_precision=item.published_at_precision)
            if document != current:
                raise ValueError("Исходные доказательства изменились после отказа")
            anchor_found |= item.id == raw_id
            result.append(document)
        if not anchor_found:
            raise ValueError("Комплект не содержит исходного материала")
        return tuple(result)

    def _error(self, raw_id: int, error: Exception, *,
               channel: Literal["primary", "rejection_audit", "delivery"]) -> dict[str, Any]:
        with Session(self.engine) as session:
            raw_id = self._target(session, raw_id)
            self._state(session, raw_id)
            if channel == "delivery":
                core.delivery_failure(session, raw_id, type(error).__name__, now=self._time())
            else:
                core.after_error(session, raw_id, type(error).__name__, now=self._time(),
                                 channel=channel)
            state = self._state(session, raw_id)
            session.commit()
        return {"raw_item_id": raw_id, "status": state["state"],
                "reason": state["reason"], "lifecycle": state}

    def _audit(self, raw_id: int) -> bool:
        with Session(self.engine) as session:
            self._state(session, raw_id)
            claimed = core.before_attempt(session, raw_id, now=self._time(),
                                          channel="rejection_audit")
            session.commit()
            if not claimed:
                return False
            run = session.get(EditorialRun, raw_id)
            if run is None or run.outcome.get("status") not in {"skipped", "blocked"}:
                raise ValueError("Отсутствует проверяемый отказ")
            ranking = RankingResult.model_validate(run.outcome.get("ranking"))
            documents = self._documents(session, raw_id, run)
        review = audit_rejection(self.provider, raw_item_id=raw_id, ranking=ranking,
            documents=documents, profile=load_city_profile(self.city), now=self._time())
        with Session(self.engine) as session:
            self._state(session, raw_id)
            core.record_rejection_review(session, raw_id, review.model_dump(mode="json"),
                                         now=self._time())
            state = core.rejection_reviewed(session, raw_id,
                justified=review.decision == "confirm_rejection", now=self._time())
            session.commit()
        return state.get("next_action") == "retry_prepare"

    def process(self, raw_id: int, delegate: Callable[[int], dict[str, Any]], *,
                retry_refuted: bool = True) -> dict[str, Any]:
        with Session(self.engine) as session:
            state = self._state(session, raw_id)
            session.commit()
        if state.get("next_action") == "audit_rejection":
            try:
                retry = self._audit(raw_id)
            except Exception as error:
                return self._error(raw_id, error, channel="rejection_audit")
            if retry and retry_refuted:
                return self.process(raw_id, delegate)
            with Session(self.engine) as session:
                state = self._state(session, raw_id)
                session.commit()
            return {"raw_item_id": raw_id, "status": state["state"],
                    "reason": state["reason"], "lifecycle": state}
        with Session(self.engine) as session:
            state = self._state(session, raw_id)
            run = session.get(EditorialRun, raw_id)
            if (state.get("next_action") == "repair" and run is not None
                    and datetime.fromisoformat(state["next_attempt_at"]) <= self._time()
                    and not eligible_policy_reassessment(session, raw_id, now=self._time())):
                state = core.block_unsupported_repair(session, raw_id, now=self._time())
                session.commit()
                return {"raw_item_id": raw_id, "status": state["state"],
                        "reason": state["reason"], "lifecycle": state}
            claimed = core.before_attempt(session, raw_id, now=self._time())
            session.commit()
            if not claimed:
                return {"raw_item_id": raw_id, "status": state["state"],
                        "reason": state["reason"], "lifecycle": state}
            run = session.get(EditorialRun, raw_id)
            if state["next_action"] == "retry_prepare" and run is not None:
                if not core.archive_for_retry(session, raw_id, now=self._time()):
                    archive_error = ValueError("Нельзя безопасно архивировать прежний результат")
                    session.rollback()
                    self._error(raw_id, archive_error, channel="primary")
                    raise archive_error
                session.commit()
        try:
            result = delegate(raw_id)
            if not isinstance(result, dict):
                raise TypeError("Обработчик должен вернуть результат")
        except Exception as error:
            self._error(raw_id, error, channel="primary")
            raise
        with Session(self.engine) as session:
            raw_id = self._target(session, raw_id)
            state = core.after_result(session, raw_id, now=self._time())
            session.commit()
        return {**result, "lifecycle": state}

    def delivery_failed(self, raw_id: int, error: Exception) -> dict[str, Any]:
        """Неизвестная отправка остаётся blocked для сверки, без повторной отправки."""
        return self._error(raw_id, error, channel="delivery")
