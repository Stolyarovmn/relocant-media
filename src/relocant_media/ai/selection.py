"""Воспроизводимые проверки структурированного отбора до извлечения утверждений."""

from relocant_media.ai.models import EvidenceDocument, RankingResult
from relocant_media.domain.city_profile import CityEditorialProfile


def selection_reason(
    ranking: RankingResult,
    profile: CityEditorialProfile,
    documents: tuple[EvidenceDocument, ...],
    *,
    evergreen: bool = False,
) -> str | None:
    """Проверить контракт отбора; содержательный интерес затем проверяет utility-роль."""
    selection = ranking.selection
    if selection is None:
        return "selection_brief_missing"
    if (selection.policy_version != profile.policy_version
            or selection.profile_sha256 != profile.sha256):
        return "selection_profile_mismatch"
    rubric = profile.rubric(selection.rubric_id)
    if rubric is None or ranking.category not in rubric.categories:
        return "selection_rubric_mismatch"
    if selection.locality == "country" and not profile.geography.allow_country_scope:
        return "selection_geography_outside_profile"
    if evergreen and selection.value_kind != "action":
        return "selection_guide_requires_practical_value"
    evidence = {(document.source_id, document.url): document for document in documents}
    for citation in selection.evidence_refs:
        document = evidence.get((citation.source_id, citation.url))
        if (document is None
                or " ".join(citation.quote.split()) not in " ".join(document.body.split())):
            return "selection_ungrounded_evidence"
    return None
