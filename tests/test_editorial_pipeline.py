from typing import Any

from pydantic import BaseModel

from relocant_media.ai.models import (
    ClaimSet,
    ClaimVerification,
    EditorialOutcome,
    EvidenceCitation,
    EvidenceDocument,
    ExtractedClaim,
    PublicationStatus,
    RankingResult,
    RussianDraft,
    VerificationResult,
)
from relocant_media.ai.pipeline import EditorialPipeline


class FakeProvider:
    def __init__(self, responses: dict[str, BaseModel]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    def generate_json(
        self,
        *,
        task: str,
        system_instruction: str,
        prompt: str,
        response_schema: dict[str, Any],
    ) -> str:
        del system_instruction, prompt, response_schema
        self.calls.append(task)
        return self._responses[task].model_dump_json()


def _document() -> EvidenceDocument:
    return EvidenceDocument(
        source_id="city_beograd_news",
        url="https://www.beograd.rs/example.html",
        title="Servisna informacija",
        body="Bez vode će biti deo Zvezdare od 08 do 18 časova.",
    )


def _ranking(category: str = "utilities") -> RankingResult:
    return RankingResult(
        should_process=True,
        relevance=0.98,
        category=category,
        confidence=0.99,
        rationale="Useful city service information.",
    )


def _claims(quote: str | None = None) -> ClaimSet:
    return ClaimSet(
        claims=(
            ExtractedClaim(
                claim_id="c1",
                text="Deo Zvezdare biće bez vode od 08 do 18 časova.",
                material=True,
                citations=(
                    EvidenceCitation(
                        source_id="city_beograd_news",
                        url="https://www.beograd.rs/example.html",
                        quote=quote or "Bez vode će biti deo Zvezdare od 08 do 18 časova.",
                    ),
                ),
            ),
        )
    )


def _verification(supported: bool = True, confidence: float = 0.99) -> VerificationResult:
    return VerificationResult(
        claims=(
            ClaimVerification(
                claim_id="c1",
                supported=supported,
                confidence=confidence,
                reason="Directly stated by the source." if supported else "Not supported.",
            ),
        )
    )


def _draft() -> RussianDraft:
    return RussianDraft(
        title="На Звездаре временно отключат воду",
        body="Часть Звездары останется без воды с 08:00 до 18:00.",
        claim_ids=("c1",),
    )


def _run(provider: FakeProvider) -> EditorialOutcome:
    return EditorialPipeline(provider).run(city="belgrade", documents=(_document(),))


def test_verified_medium_risk_item_is_ready_at_high_confidence() -> None:
    provider = FakeProvider(
        {
            "rank": _ranking(),
            "claims": _claims(),
            "verify": _verification(),
            "draft": _draft(),
        }
    )

    result = _run(provider)

    assert result.status is PublicationStatus.READY
    assert result.draft is not None
    assert provider.calls == ["rank", "claims", "verify", "draft"]


def test_high_risk_category_always_requires_human_review() -> None:
    provider = FakeProvider(
        {
            "rank": _ranking("immigration"),
            "claims": _claims(),
            "verify": _verification(),
            "draft": _draft(),
        }
    )

    result = _run(provider)

    assert result.status is PublicationStatus.HUMAN_REVIEW
    assert result.reason == "policy_requires_human_review"


def test_unsupported_material_claim_blocks_before_drafting() -> None:
    provider = FakeProvider(
        {
            "rank": _ranking("city"),
            "claims": _claims(),
            "verify": _verification(supported=False),
        }
    )

    result = _run(provider)

    assert result.status is PublicationStatus.BLOCKED
    assert result.reason == "unsupported_material_claim"
    assert result.draft is None
    assert provider.calls == ["rank", "claims", "verify"]


def test_fabricated_citation_blocks_before_verification() -> None:
    provider = FakeProvider(
        {
            "rank": _ranking("city"),
            "claims": _claims("Ovaj citat ne postoji u izvornom tekstu."),
        }
    )

    result = _run(provider)

    assert result.status is PublicationStatus.BLOCKED
    assert result.reason == "ungrounded_citation"
    assert provider.calls == ["rank", "claims"]


def test_ranking_can_skip_irrelevant_item_without_more_model_calls() -> None:
    ranking = RankingResult(
        should_process=False,
        relevance=0.05,
        category="other",
        confidence=0.99,
        rationale="Not locally useful.",
    )
    provider = FakeProvider({"rank": ranking})

    result = _run(provider)

    assert result.status is PublicationStatus.SKIPPED
    assert provider.calls == ["rank"]
