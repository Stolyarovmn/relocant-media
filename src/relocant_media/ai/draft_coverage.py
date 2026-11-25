"""Проверяемый контракт покрытия точного текста независимым фактическим ревью."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from relocant_media.ai.evidence_scope import (
    EVENT_SCOPE_INSTRUCTION,
    PRACTICAL_IMPACT_INSTRUCTION,
    UTILITY_PRECISION_INSTRUCTION,
    unsupported_action_traffic_effect,
)
from relocant_media.ai.linked_update import LinkedUpdateFactsCheck
from relocant_media.ai.models import (
    ClaimSet,
    EvidenceCitation,
    EvidenceDocument,
    RussianDraft,
    VerificationResult,
)


class CoverageCitation(EvidenceCitation):
    model_config = ConfigDict(strict=True, extra="forbid")


class DraftCoverageSegment(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    field: Literal["title", "body"]
    text: str = Field(min_length=1)
    claim_ids: tuple[str, ...]
    citations: tuple[CoverageCitation, ...]
    assessment: Literal["supported", "unsupported", "uncertain"]


class DraftFactsCheck(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    policy_version: Literal["draft-coverage-v1"]
    segments: tuple[DraftCoverageSegment, ...] = Field(min_length=2)
    publication_scope: Literal[
        "ordinary_local", "confirmed_transport_notice", "sensitive_substance", "uncertain",
    ]
    scope_claim_ids: tuple[str, ...]
    reason: str = Field(min_length=1)
    linked_update_check: LinkedUpdateFactsCheck | None = None

    @model_serializer(mode="wrap")
    def serialize_compatible(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload: dict[str, Any] = handler(self)
        if self.linked_update_check is None:
            payload.pop("linked_update_check", None)
        return payload


def _citation_key(citation: EvidenceCitation) -> tuple[str, str, str]:
    return citation.source_id, citation.url, citation.quote


def draft_coverage_reason(
    check: DraftFactsCheck | None, draft: RussianDraft, claims: ClaimSet,
    verification: VerificationResult, documents: tuple[EvidenceDocument, ...], category: str,
) -> str | None:
    """Проверить полноту/ссылки; смысловое следование оценивает независимый reviewer."""
    if check is None:
        return "draft_coverage_required"
    for field in ("title", "body"):
        if "".join(segment.text for segment in check.segments if segment.field == field) != (
            getattr(draft, field)):
            return "draft_coverage_incomplete"
    by_id = {claim.claim_id: claim for claim in claims.claims}
    supported = {item.claim_id for item in verification.claims if item.supported}
    allowed = set(draft.claim_ids) & supported & set(by_id)
    by_source = {(document.source_id, document.url): document for document in documents}
    sources = tuple(f"{document.title}\n{document.body}" for document in documents)
    covered_ids: set[str] = set()
    for segment in check.segments:
        if segment.assessment != "supported":
            return "draft_coverage_unsupported"
        if segment.text.isspace() and not segment.claim_ids and not segment.citations:
            continue
        if (not segment.claim_ids or len(set(segment.claim_ids)) != len(segment.claim_ids)
                or not set(segment.claim_ids).issubset(allowed)):
            return "draft_coverage_invalid_claim"
        covered_ids.update(segment.claim_ids)
        citations = {_citation_key(citation) for claim_id in segment.claim_ids
                     for citation in by_id[claim_id].citations}
        checked = {_citation_key(citation) for citation in segment.citations}
        if not checked or not checked.issubset(citations):
            return "draft_coverage_ungrounded"
        # Каждый указанный claim должен иметь собственную цитату в этом сегменте.
        if any(not checked.intersection(_citation_key(citation)
                   for citation in by_id[claim_id].citations) for claim_id in segment.claim_ids):
            return "draft_coverage_ungrounded"
        for source_id, url, quote in checked:
            document = by_source.get((source_id, url))
            if document is None or not quote.strip() or quote not in document.body:
                return "draft_coverage_ungrounded"
        if unsupported_action_traffic_effect(
            segment.text, quotes=(quote for _, _, quote in checked), sources=sources,
        ):
            return "draft_coverage_unsupported"
    if (len(set(check.scope_claim_ids)) != len(check.scope_claim_ids)
            or not set(check.scope_claim_ids).issubset(covered_ids)):
        return "draft_coverage_invalid_claim"
    if check.publication_scope == "uncertain":
        return "independent_risk_classification_required"
    if check.publication_scope == "sensitive_substance":
        return "independent_sensitivity_review_required"
    if check.publication_scope == "confirmed_transport_notice" and (
            category.strip().lower() != "transport" or not check.scope_claim_ids):
        return "independent_risk_classification_required"
    return None


FACTS_COVERAGE_INSTRUCTION = (
    "Обязателен facts_check версии draft-coverage-v1. Независимо разложи весь точный "
    "draft.title и draft.body на последовательные минимальные фрагменты; каждое "
    "атомарное утверждение и отдельный причинный вывод проверяй отдельно. text копируй "
    "посимвольно: конкатенация segments каждого field должна полностью воспроизводить "
    "это поле, включая пробелы и переносы. Для каждого содержательного фрагмента укажи "
    "только реально использованные draft.claim_ids с supported verification и точные "
    "citations этих claims (source_id, URL, непрерывная дословная quote из evidence.body). "
    "Проверь смысловое следование фрагмента именно из этих claims и цитат, а не просто "
    "их тематическое сходство. Несуществующая связь или новое существенное последствие "
    "означает unsupported/uncertain даже при правдоподобии. Нельзя охватить одним "
    "фрагментом несколько выводов и скрыть неподтверждённый среди подтверждённых. "
    "Отсутствующий claim нельзя заменить любым подходящим отрывком evidence. "
    "selection.benefit, ranking.rationale, категория и предыдущие оценки — не источники. "
    "Только чистые пробелы и переносы могут иметь пустые claim_ids/citations; нельзя "
    "пометить произвольный текст не-фактом. Атрибутированное мнение допустимо, если "
    "источник подтверждает автора и само высказывание, а draft сохраняет атрибуцию. "
    "Отдельно независимо оцени publication_scope по смыслу всего текста и источников, "
    "не доверяя ranking.category: ordinary_local для низкорисковых городских, культурных "
    "и спортивных историй; sensitive_substance для правовых, политических и иных "
    "чувствительных требований/советов; uncertain при неясной границе. Подтверждённое "
    "транспортное изменение можно отметить confirmed_transport_notice: scope_claim_ids "
    "обязаны доказывать само изменение, а не только место/время соседнего события. "
    "Наличие митинга, шествия или собрания само по себе не доказывает ограничение "
    "движения. Однако само упоминание протеста не делает подтверждённое транспортное "
    "объявление политическим: если текст ограничен доказанным изменением движения, "
    "оно допустимо. Нельзя скрывать обсуждение правовых/политических требований "
    "переименованием в transport. Для культуры/спорта транспортная польза не требуется. "
    "Любой unsupported/uncertain фрагмент требует hold/reject и блокирующего flags. "
    "Если факты полностью подтверждены, но тема чувствительная, возможен approve "
    "по фактам с sensitive_substance: автоматическая публикация всё равно запрещена, "
    "решение о риске принимает владелец отдельно."
) + EVENT_SCOPE_INSTRUCTION + PRACTICAL_IMPACT_INSTRUCTION + UTILITY_PRECISION_INSTRUCTION


FACTS_COVERAGE_INSTRUCTION += (
    " Если пакет содержит linked_update, независимо открой прежнюю публичную новость "
    "с указанным message_id в собственном канале и сопоставь событие и новые факты. "
    "Верни linked_update_check: parent_message_id, parent_publication_checked, same_event, "
    "substantive_new_facts, точный change_claim_ids и reason. Только новые подтверждённые "
    "draft claims могут доказывать существенное дополнение. Совпадение темы или города "
    "не доказывает то же событие. При недоступности прежней публикации, иной теме или "
    "повторе без нового факта — hold, положительную связь не имитировать. "
    "Каноническая ссылка доказывает квитанцию, но не семантическую связь: её проверяет reviewer. "
    "Если linked_update отсутствует или равен null, linked_update_check не передавай "
    "или оставь null; не создавай формальный объект с false для обычной новости."
)
