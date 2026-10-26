import json
from collections.abc import Iterable

from pydantic import BaseModel, ValidationError

from relocant_media.ai.models import (
    ClaimSet,
    EditorialOutcome,
    EvidenceDocument,
    PublicationStatus,
    RankingResult,
    RussianDraft,
    VerificationResult,
)
from relocant_media.ai.prompts import (
    CLAIMS_SYSTEM,
    DRAFT_SYSTEM,
    RANK_SYSTEM,
    VERIFY_SYSTEM,
)
from relocant_media.ai.provider import StructuredAIProvider, StructuredOutputError
from relocant_media.domain.audience import audience_relevance_reason
from relocant_media.domain.freshness import publication_date_context
from relocant_media.domain.policy import requires_human_review


def _json_payload(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _documents_payload(documents: tuple[EvidenceDocument, ...]) -> list[dict[str, object]]:
    return [document.model_dump(mode="json") for document in documents]


def _ask[ModelT: BaseModel](
    provider: StructuredAIProvider,
    *,
    task: str,
    system_instruction: str,
    payload: object,
    response_model: type[ModelT],
) -> ModelT:
    raw = provider.generate_json(
        task=task,
        system_instruction=system_instruction,
        prompt=_json_payload(payload),
        response_schema=response_model.model_json_schema(),
    )
    try:
        return response_model.model_validate_json(raw)
    except ValidationError as error:
        raise StructuredOutputError(f"invalid structured output for {task}") from error


def _citations_are_grounded(
    claims: ClaimSet,
    documents: tuple[EvidenceDocument, ...],
) -> bool:
    by_source = {document.source_id: document for document in documents}
    if len(by_source) != len(documents):
        return False

    claim_ids = [claim.claim_id for claim in claims.claims]
    if len(set(claim_ids)) != len(claim_ids):
        return False

    for claim in claims.claims:
        for citation in claim.citations:
            document = by_source.get(citation.source_id)
            if document is None or citation.url != document.url:
                return False
            quote = _normalize_text(citation.quote)
            evidence = _normalize_text(document.body)
            if not quote or quote not in evidence:
                return False
    return True


def _verification_map(
    verification: VerificationResult,
    claims: ClaimSet,
) -> dict[str, tuple[bool, float]] | None:
    expected = {claim.claim_id for claim in claims.claims}
    verdicts = {verdict.claim_id: verdict for verdict in verification.claims}
    if len(verdicts) != len(verification.claims) or set(verdicts) != expected:
        return None
    return {
        claim_id: (verdict.supported, verdict.confidence)
        for claim_id, verdict in verdicts.items()
    }


def _minimum_confidence(values: Iterable[float], fallback: float) -> float:
    candidates = list(values)
    return min(candidates) if candidates else fallback


class EditorialPipeline:
    def __init__(self, provider: StructuredAIProvider) -> None:
        self._provider = provider

    def run(
        self,
        *,
        city: str,
        documents: tuple[EvidenceDocument, ...],
    ) -> EditorialOutcome:
        if not documents:
            raise ValueError("at least one evidence document is required")

        if city == "belgrade":
            reason = next((reason for document in documents
                           if (reason := audience_relevance_reason(document.title, document.body))),
                          None)
            if reason:
                return EditorialOutcome(status=PublicationStatus.SKIPPED, reason=reason,
                    ranking=RankingResult(should_process=False, relevance=0, category="events",
                                          confidence=1, rationale=reason), confidence=1)

        evidence = _documents_payload(documents)
        ranking = _ask(
            self._provider,
            task="rank",
            system_instruction=RANK_SYSTEM,
            payload={"city": city, "evidence": evidence},
            response_model=RankingResult,
        )
        if not ranking.should_process:
            return EditorialOutcome(
                status=PublicationStatus.SKIPPED,
                reason="ranking_rejected",
                ranking=ranking,
                confidence=ranking.confidence,
            )

        claims = _ask(
            self._provider,
            task="claims",
            system_instruction=CLAIMS_SYSTEM,
            payload={"city": city, "category": ranking.category, "evidence": evidence},
            response_model=ClaimSet,
        )
        if not _citations_are_grounded(claims, documents):
            return EditorialOutcome(
                status=PublicationStatus.BLOCKED,
                reason="ungrounded_citation",
                ranking=ranking,
                claims=claims,
                confidence=0.0,
            )

        verification = _ask(
            self._provider,
            task="verify",
            system_instruction=VERIFY_SYSTEM,
            payload={"evidence": evidence, "claims": claims.model_dump(mode="json")},
            response_model=VerificationResult,
        )
        verdicts = _verification_map(verification, claims)
        if verdicts is None:
            return EditorialOutcome(
                status=PublicationStatus.BLOCKED,
                reason="verification_contract_mismatch",
                ranking=ranking,
                claims=claims,
                verification=verification,
                confidence=0.0,
            )

        unsupported_material = [
            claim.claim_id
            for claim in claims.claims
            if claim.material and not verdicts[claim.claim_id][0]
        ]
        verification_confidence = _minimum_confidence(
            (verdicts[claim.claim_id][1] for claim in claims.claims),
            ranking.confidence,
        )
        overall_confidence = min(ranking.confidence, verification_confidence)
        if unsupported_material:
            return EditorialOutcome(
                status=PublicationStatus.BLOCKED,
                reason="unsupported_material_claim",
                ranking=ranking,
                claims=claims,
                verification=verification,
                confidence=overall_confidence,
            )

        supported_claim_ids = {
            claim.claim_id for claim in claims.claims if verdicts[claim.claim_id][0]
        }
        draft = _ask(
            self._provider,
            task="draft",
            system_instruction=DRAFT_SYSTEM,
            payload={
                "city": city,
                "category": ranking.category,
                "source_local_dates":[dict(source_id=d.source_id,
                    **publication_date_context(d.published_at, d.published_at_precision))
                    for d in documents],
                "claims": [
                    claim.model_dump(mode="json")
                    for claim in claims.claims
                    if claim.claim_id in supported_claim_ids
                ],
            },
            response_model=RussianDraft,
        )
        if not set(draft.claim_ids).issubset(supported_claim_ids):
            return EditorialOutcome(
                status=PublicationStatus.BLOCKED,
                reason="draft_references_unverified_claim",
                ranking=ranking,
                claims=claims,
                verification=verification,
                draft=draft,
                confidence=overall_confidence,
            )

        status = PublicationStatus.READY
        reason = "verified"
        if requires_human_review(ranking.category, overall_confidence):
            status = PublicationStatus.HUMAN_REVIEW
            reason = "policy_requires_human_review"

        return EditorialOutcome(
            status=status,
            reason=reason,
            ranking=ranking,
            claims=claims,
            verification=verification,
            draft=draft,
            confidence=overall_confidence,
        )
