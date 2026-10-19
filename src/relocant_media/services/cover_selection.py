"""Deterministic cover selection from explicit, auditable editorial assessments."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CoverAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    image_url: str = Field(min_length=8)
    source_page: str = Field(min_length=8)
    kind: Literal["official_poster", "current_photo", "archive_photo", "logo", "unknown"]
    event_match: bool
    event_year: int | None = Field(default=None, ge=1900, le=2100)
    visual_value: int = Field(ge=0, le=4)
    distinct_visual_value: bool = False
    rights_method: Literal["permission", "license", "current_event_reporting", "unknown"]
    rights_status: Literal["reviewed", "needs_review", "rejected"]
    rights_evidence_url: str = ""
    rights_rationale: str = ""
    attribution: str = ""
    archive_label: str = ""
    assessed_by: str = Field(min_length=1)


def plan_cover(
    assessments: tuple[CoverAssessment, ...], *, event_year: int, allow_album: bool = False,
) -> dict[str, object]:
    eligible: list[CoverAssessment] = []
    rejected = []
    seen = set()
    for item in assessments:
        reason = ""
        if item.image_url in seen:
            reason = "duplicate_url"
        elif item.kind in {"unknown", "logo"} or not item.event_match:
            reason = "unconfirmed_event_image"
        elif item.visual_value < 2:
            reason = "insufficient_visual_value"
        elif (item.rights_status != "reviewed" or item.rights_method == "unknown"
              or not item.rights_evidence_url or not item.rights_rationale or not item.attribution):
            reason = "rights_review_required"
        elif item.event_year != event_year and item.kind != "archive_photo":
            reason = "event_year_mismatch"
        elif item.kind == "archive_photo" and (
            item.event_year is None or item.event_year >= event_year
            or str(item.event_year) not in item.archive_label
        ):
            reason = "archive_year_and_label_required"
        if reason:
            rejected.append({"image_url": item.image_url, "reason": reason})
        else:
            eligible.append(item)
        seen.add(item.image_url)
    priority = {"official_poster": 0, "current_photo": 1, "archive_photo": 2}
    eligible.sort(key=lambda item: (priority[item.kind], -item.visual_value, item.image_url))
    selected = eligible[:1]
    if allow_album:
        photos = [a for a in eligible if a.kind in {"current_photo", "archive_photo"}
                  and a.distinct_visual_value]
        if len(photos) >= 2:
            selected = photos[:5]
    return {
        "version": 1,
        "mode": "album" if len(selected) > 1 else "single_cover" if selected else "text",
        "selected": [item.model_dump(mode="json") for item in selected],
        "rejected": rejected,
        "reason": "reviewed_event_visual" if selected else "no_suitable_cover_publish_text",
        "requires_independent_media_reviews": bool(selected),
    }
