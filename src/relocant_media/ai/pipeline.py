import json
from collections.abc import Iterable
from datetime import UTC, datetime

from pydantic import BaseModel, ValidationError

from relocant_media.ai.audience_policy import profile_audience_policy, profile_draft_style
from relocant_media.ai.channel_style import writing_context, writing_instruction
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
    DRAFT_BASE_SYSTEM,
    DRAFT_SYSTEM,
    PROFILE_RANK_SYSTEM,
    RANK_EVERGREEN,
    RANK_SYSTEM,
    VERIFY_SYSTEM,
)
from relocant_media.ai.provider import StructuredAIProvider, StructuredOutputError
from relocant_media.ai.selection import selection_reason
from relocant_media.domain.audience import (
    audience_relevance_reason,
    local_flight_change,
    local_utility_notice,
)
from relocant_media.domain.city_profile import CityEditorialProfile
from relocant_media.domain.draft_language import draft_language_reason
from relocant_media.domain.freshness import publication_date_context
from relocant_media.domain.policy import is_borderline, requires_human_review


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


DRAFT_LANGUAGE_RETRY_NOTE = (
    "The previous draft was not in Russian (Serbian text was left untranslated). Write the "
    "title and body in Russian; keep only proper names in their original spelling."
)

CITATION_RETRY_NOTE = (
    "Previous quotes were not found verbatim in the evidence. Copy each quote character for "
    "character from one contiguous passage; no ellipsis, no merged passages, no corrected "
    "spelling or punctuation."
)

FLIGHT_RETRY_NOTE = (
    "Повторно оцени подтверждённое изменение рейсов по пользе для пассажиров, "
    "которых оно затрагивает. Проверь связь маршрута с городом и категорию transport. "
    "При положительном решении приложи точные выдержки из источника."
)

UTILITY_RETRY_NOTE = (
    "Повторно оцени официальное отключение для жителей конкретных адресов. "
    "Выбери utilities и процитируй короткие дословные выдержки про место и время работ."
)


def _ungrounded_quotes(
    claims: ClaimSet,
    documents: tuple[EvidenceDocument, ...],
) -> list[str]:
    bodies = {document.source_id: _normalize_text(document.body) for document in documents}
    return [
        citation.quote
        for claim in claims.claims
        for citation in claim.citations
        if _normalize_text(citation.quote) not in bodies.get(citation.source_id, "")
    ]


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
    def __init__(
        self, provider: StructuredAIProvider, *, city_profile: CityEditorialProfile | None = None,
    ) -> None:
        self._provider = provider
        self.city_profile = city_profile

    def _ask_draft(self, payload: dict[str, object], style: str) -> RussianDraft:
        return _ask(
            self._provider,
            task="draft",
            system_instruction=(DRAFT_BASE_SYSTEM + profile_draft_style(self.city_profile)
                                if self.city_profile else DRAFT_SYSTEM) + style,
            payload=payload,
            response_model=RussianDraft,
        )

    def run(
        self,
        *,
        city: str,
        documents: tuple[EvidenceDocument, ...],
        evergreen: bool = False,
        recent_publications: list[dict[str, str]] | None = None,
        now: datetime | None = None,
        source_update: bool = False,
    ) -> EditorialOutcome:
        if not documents:
            raise ValueError("at least one evidence document is required")
        profile = self.city_profile
        if profile is not None and profile.city_id != city:
            raise ValueError("Город материала не совпадает с редакционным профилем")

        if city == "belgrade":
            reason = next((reason for document in documents
                           if (reason := audience_relevance_reason(document.title, document.body))),
                          None)
            if reason:
                return EditorialOutcome(status=PublicationStatus.SKIPPED, reason=reason,
                    ranking=RankingResult(should_process=False, relevance=0, category="events",
                                          confidence=1, rationale=reason), confidence=1)

        evidence = _documents_payload(documents)
        rank_instruction = (
            PROFILE_RANK_SYSTEM + profile_audience_policy(profile) if profile else RANK_SYSTEM
        )
        profile_payload = ({"city_profile": profile.model_dump(mode="json"),
                            "profile_sha256": profile.sha256} if profile else {})
        rank_payload = {"city": city, "evidence": evidence, **profile_payload,
                        **({"recent_publications": recent_publications}
                           if recent_publications is not None else {}),
                        **({"content_type": "evergreen_guide"} if evergreen else {})}

        def rank(correction: str | None = None) -> RankingResult:
            return _ask(
                self._provider, task="rank",
                system_instruction=rank_instruction + (RANK_EVERGREEN if evergreen else ""),
                payload={**rank_payload, **({"correction": correction} if correction else {})},
                response_model=RankingResult,
            )

        ranking = rank()
        if profile is not None:
            rubric = profile.rubric("city_changes")
            flight_change = (
                not evergreen and rubric is not None and "transport" in rubric.categories
                and any(local_flight_change(d.title, d.body, profile.geography.city_aliases)
                        for d in documents)
            )
            utility_notice = (
                not evergreen and rubric is not None and "utilities" in rubric.categories
                and any(local_utility_notice(d.source_id, d.url, d.title, d.body)
                        for d in documents)
            )
            expected_category = (
                "utilities" if utility_notice else "transport" if flight_change else None
            )
            reason = (selection_reason(ranking, profile, documents, evergreen=evergreen)
                      if ranking.should_process else None)
            corrections = []
            if reason == "selection_ungrounded_evidence":
                corrections.append(CITATION_RETRY_NOTE)
            if expected_category and (not ranking.should_process
                                      or ranking.category != expected_category):
                corrections.append(UTILITY_RETRY_NOTE if utility_notice else FLIGHT_RETRY_NOTE)
            if corrections:
                corrected = rank(" ".join(corrections))
                # A rejected retry cannot erase an accepted topic's review requirement.
                if corrected.should_process or not ranking.should_process:
                    ranking = corrected
                    reason = (selection_reason(ranking, profile, documents, evergreen=evergreen)
                              if ranking.should_process else None)
            if expected_category and (not ranking.should_process
                                      or ranking.category != expected_category):
                reason = ("local_utility_notice_requires_review" if utility_notice
                          else "local_flight_change_requires_review")
            if reason:
                return EditorialOutcome(status=PublicationStatus.HUMAN_REVIEW, reason=reason,
                    ranking=ranking, confidence=ranking.confidence)
            if not ranking.should_process:
                return EditorialOutcome(status=PublicationStatus.SKIPPED, reason="ranking_rejected",
                    ranking=ranking, confidence=ranking.confidence)
        borderline = not ranking.should_process and is_borderline(
            ranking.relevance, ranking.confidence, ranking.category,
        )
        if not ranking.should_process and not borderline:
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
            claims = _ask(
                self._provider,
                task="claims",
                system_instruction=CLAIMS_SYSTEM,
                payload={
                    "city": city,
                    "category": ranking.category,
                    "evidence": evidence,
                    "correction": CITATION_RETRY_NOTE,
                    "ungrounded_quotes": _ungrounded_quotes(claims, documents),
                },
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
        draft_payload: dict[str, object] = {
            "city": city,
            "category": ranking.category,
            "writing_calendar": writing_context(profile, now or datetime.now(UTC)),
            **profile_payload,
            **({"selection": ranking.selection.model_dump(mode="json")}
               if ranking.selection else {}),
            "source_local_dates":[dict(source_id=d.source_id,
                **publication_date_context(d.published_at, d.published_at_precision,
                    timezone_name=profile.timezone if profile else None))
                for d in documents],
            "claims": [
                claim.model_dump(mode="json")
                for claim in claims.claims
                if claim.claim_id in supported_claim_ids
            ],
        }
        style = writing_instruction(ranking.category, evergreen=evergreen)
        if source_update:
            style += (
                "\nЭто подтверждённая новая версия источника ранее опубликованной новости. "
                "Вынеси изменение в заголовок, в начале текста дай актуальные дату, часы или "
                "границы. Форму «было → стало» используй только для фактов, подтверждённых "
                "текстом текущего источника. Не додумывай прежние условия."
            )
        draft = self._ask_draft(draft_payload, style)
        if draft_language_reason(draft.title, draft.body) is not None:
            draft = self._ask_draft(
                {**draft_payload, "correction": DRAFT_LANGUAGE_RETRY_NOTE}, style,
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
        elif borderline:
            status = PublicationStatus.HUMAN_REVIEW
            reason = "borderline_relevance"

        return EditorialOutcome(
            status=status,
            reason=reason,
            ranking=ranking,
            claims=claims,
            verification=verification,
            draft=draft,
            confidence=overall_confidence,
        )
