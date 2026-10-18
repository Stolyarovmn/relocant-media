from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class RiskClass(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SubmissionKind(StrEnum):
    NEWS_TIP = "news_tip"
    CORRECTION = "correction"
    AD_REQUEST = "ad_request"
    EVENT = "event"
    QUESTION = "question"


@dataclass(frozen=True, slots=True)
class SourceRef:
    url: str
    title: str
    publisher: str
    retrieved_at: datetime


@dataclass(frozen=True, slots=True)
class Claim:
    text: str
    sources: tuple[SourceRef, ...]
    confidence: float
