"""Non-authoritative duplicate discovery for measuring misses in deterministic dedup.

Nothing in this module may decide publication eligibility or merge events. The default
scorer is deliberately local and dependency-free; a future embedding scorer can implement
the same protocol without changing the authoritative ``same_event`` path.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from relocant_media.domain.dedup import EventCandidate, normalize_text, same_event

DEFAULT_SHADOW_WINDOW = timedelta(hours=72)
DEFAULT_SHADOW_THRESHOLD = 0.68

_CYRILLIC_TO_LATIN = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "ђ": "dj",
        "е": "e", "ё": "e", "ж": "z", "з": "z", "и": "i", "й": "j",
        "ј": "j", "к": "k", "л": "l", "љ": "lj", "м": "m", "н": "n",
        "њ": "nj", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
        "ћ": "c", "у": "u", "ф": "f", "х": "h", "ц": "c", "ч": "c",
        "џ": "dz", "ш": "s", "ы": "y", "э": "e", "ю": "ju", "я": "ja",
        "ь": "", "ъ": "",
    }
)
_NUMBER = re.compile(r"\b\d+[\w.-]*\b", re.UNICODE)
_STOPWORDS = {
    "a", "ali", "and", "at", "da", "do", "for", "i", "in", "is", "iz", "je",
    "na", "od", "of", "po", "sa", "se", "the", "u", "za", "и", "в", "во", "на",
    "по", "с", "со", "для", "из", "от", "до", "о", "об", "это", "будет",
}


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().translate(_CYRILLIC_TO_LATIN)
    value = "".join(
        char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char)
    )
    return normalize_text(value)


def _tokens(value: str) -> set[str]:
    return {token for token in _fold(value).split() if len(token) > 1 and token not in _STOPWORDS}


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _ngrams(value: str, size: int = 4) -> Counter[str]:
    compact = _fold(value).replace(" ", "_")
    if len(compact) < size:
        return Counter({compact: 1}) if compact else Counter()
    return Counter(compact[index : index + size] for index in range(len(compact) - size + 1))


def _cosine(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(count * right.get(key, 0) for key, count in left.items())
    if dot == 0:
        return 0.0
    left_norm = math.sqrt(sum(count * count for count in left.values()))
    right_norm = math.sqrt(sum(count * count for count in right.values()))
    return dot / (left_norm * right_norm)


def _anchors(value: str) -> set[str]:
    return set(_NUMBER.findall(_fold(value)))


@dataclass(frozen=True, slots=True)
class ShadowScore:
    score: float
    title_overlap: float
    body_overlap: float
    character_similarity: float
    anchor_overlap: float


@dataclass(frozen=True, slots=True)
class ShadowScorerMetadata:
    """Stable provenance for one score space.

    Embedding-backed scorers must identify both model and vector dimension. This prevents
    labels/metrics from silently mixing scores produced by incompatible representations.
    """

    scorer_id: str
    scorer_version: str
    embedding_model: str | None = None
    embedding_dimension: int | None = None

    def __post_init__(self) -> None:
        if not self.scorer_id.strip() or not self.scorer_version.strip():
            raise ValueError("shadow scorer id and version are required")
        if (self.embedding_model is None) != (self.embedding_dimension is None):
            raise ValueError("embedding model and dimension must be declared together")
        if self.embedding_dimension is not None and self.embedding_dimension < 1:
            raise ValueError("embedding dimension must be positive")


class ShadowSemanticScorer(Protocol):
    @property
    def metadata(self) -> ShadowScorerMetadata: ...

    def score(self, left: EventCandidate, right: EventCandidate) -> ShadowScore: ...


class LexicalSemanticScorer:
    """Dependency-free baseline for shadow observation, not event merging."""

    metadata = ShadowScorerMetadata(
        scorer_id="lexical_semantic",
        scorer_version="1",
    )

    def score(self, left: EventCandidate, right: EventCandidate) -> ShadowScore:
        title_overlap = _jaccard(_tokens(left.title), _tokens(right.title))
        body_overlap = _jaccard(_tokens(left.body), _tokens(right.body))
        character_similarity = _cosine(
            _ngrams(f"{left.title} {left.body[:800]}"),
            _ngrams(f"{right.title} {right.body[:800]}"),
        )
        left_anchors = _anchors(f"{left.title} {left.body}")
        right_anchors = _anchors(f"{right.title} {right.body}")
        anchor_overlap = (
            _jaccard(left_anchors, right_anchors)
            if left_anchors or right_anchors
            else 0.0
        )
        score = (
            0.50 * title_overlap
            + 0.20 * body_overlap
            + 0.25 * character_similarity
            + 0.05 * anchor_overlap
        )
        return ShadowScore(
            score=round(score, 6),
            title_overlap=round(title_overlap, 6),
            body_overlap=round(body_overlap, 6),
            character_similarity=round(character_similarity, 6),
            anchor_overlap=round(anchor_overlap, 6),
        )


@dataclass(frozen=True, slots=True)
class ShadowDuplicateSignal:
    left_raw_item_id: int
    right_raw_item_id: int
    left_source_id: str
    right_source_id: str
    score: ShadowScore
    threshold: float
    scorer: ShadowScorerMetadata

    @property
    def possible_duplicate(self) -> bool:
        return self.score.score >= self.threshold


@dataclass(frozen=True, slots=True)
class ShadowDuplicateLabel:
    left_raw_item_id: int
    right_raw_item_id: int
    duplicate: bool

    @property
    def key(self) -> tuple[int, int]:
        if self.left_raw_item_id <= self.right_raw_item_id:
            return self.left_raw_item_id, self.right_raw_item_id
        return self.right_raw_item_id, self.left_raw_item_id


def find_shadow_duplicates(
    candidates: list[EventCandidate],
    *,
    scorer: ShadowSemanticScorer | None = None,
    threshold: float = DEFAULT_SHADOW_THRESHOLD,
    window: timedelta = DEFAULT_SHADOW_WINDOW,
    limit: int = 100,
) -> list[ShadowDuplicateSignal]:
    """Return possible duplicates that authoritative dedup deliberately did not merge."""
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("threshold must be between 0 and 1")
    if limit < 1:
        raise ValueError("limit must be positive")
    scorer = scorer or LexicalSemanticScorer()
    scorer_metadata = scorer.metadata
    # Force validation at the scoring boundary, including custom/pluggable scorers.
    if not isinstance(scorer_metadata, ShadowScorerMetadata):
        raise ValueError("shadow scorer must expose validated metadata")

    dated = sorted(
        (candidate for candidate in candidates if candidate.published_at is not None),
        key=lambda item: (item.published_at, item.raw_item_id),
    )
    signals: list[ShadowDuplicateSignal] = []
    for index, left in enumerate(dated):
        assert left.published_at is not None
        for right in dated[index + 1 :]:
            assert right.published_at is not None
            if right.published_at - left.published_at > window:
                break
            if same_event(left, right):
                continue
            score = scorer.score(left, right)
            if score.score < threshold:
                continue
            signals.append(
                ShadowDuplicateSignal(
                    left_raw_item_id=left.raw_item_id,
                    right_raw_item_id=right.raw_item_id,
                    left_source_id=left.source_id,
                    right_source_id=right.source_id,
                    score=score,
                    threshold=threshold,
                    scorer=scorer_metadata,
                )
            )
    signals.sort(
        key=lambda item: (
            -item.score.score,
            item.left_raw_item_id,
            item.right_raw_item_id,
        )
    )
    return signals[:limit]


def evaluate_shadow_labels(
    signals: list[ShadowDuplicateSignal],
    labels: list[ShadowDuplicateLabel],
) -> dict[str, float | int | str | None]:
    """Evaluate reviewed pairs so the shadow threshold can be measured, not guessed."""
    scorer_metadata = {signal.scorer for signal in signals}
    if len(scorer_metadata) > 1:
        raise ValueError("shadow signals from different scorer spaces cannot be evaluated together")
    scorer = next(iter(scorer_metadata), None)

    predicted = {
        tuple(sorted((signal.left_raw_item_id, signal.right_raw_item_id)))
        for signal in signals
        if signal.possible_duplicate
    }
    truth = {label.key: label.duplicate for label in labels}
    true_positive = sum(key in predicted and duplicate for key, duplicate in truth.items())
    false_positive = sum(key in predicted and not duplicate for key, duplicate in truth.items())
    false_negative = sum(key not in predicted and duplicate for key, duplicate in truth.items())
    true_negative = sum(key not in predicted and not duplicate for key, duplicate in truth.items())

    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    negative_denominator = false_positive + true_negative
    return {
        "scorer_id": scorer.scorer_id if scorer is not None else None,
        "scorer_version": scorer.scorer_version if scorer is not None else None,
        "embedding_model": scorer.embedding_model if scorer is not None else None,
        "embedding_dimension": scorer.embedding_dimension if scorer is not None else None,
        "labels": len(labels),
        "true_positive": true_positive,
        "false_positive": false_positive,
        "false_negative": false_negative,
        "true_negative": true_negative,
        "precision": true_positive / precision_denominator if precision_denominator else None,
        "recall": true_positive / recall_denominator if recall_denominator else None,
        "false_positive_rate": (
            false_positive / negative_denominator if negative_denominator else None
        ),
        "false_negative_rate": false_negative / recall_denominator if recall_denominator else None,
    }
