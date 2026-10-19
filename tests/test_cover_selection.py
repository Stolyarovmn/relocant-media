from relocant_media.services.cover_selection import CoverAssessment, plan_cover


def assessment(**updates) -> CoverAssessment:
    return CoverAssessment.model_validate({
        "image_url": "https://example.org/poster.jpg", "source_page": "https://example.org/news",
        "kind": "official_poster", "event_match": True, "event_year": 2026, "visual_value": 4,
        "rights_method": "current_event_reporting", "rights_status": "reviewed",
        "rights_evidence_url": "https://example.org/legal-review",
        "rights_rationale": "Явное основание",
        "attribution": "Афиша: организатор", "assessed_by": "editor", **updates,
    })


def test_default_prefers_one_official_poster_over_multiple_photos() -> None:
    photo = assessment(kind="current_photo", image_url="https://example.org/photo.jpg")
    result = plan_cover((photo, assessment()), event_year=2026)
    assert result["mode"] == "single_cover"
    assert result["selected"][0]["kind"] == "official_poster"


def test_official_origin_is_not_an_automatic_rights_approval() -> None:
    result = plan_cover((assessment(rights_status="needs_review"),), event_year=2026)
    assert result["mode"] == "text"
    assert result["rejected"][0]["reason"] == "rights_review_required"


def test_wrong_year_logo_and_unknown_are_not_covers() -> None:
    for updates in ({"event_year": 2025}, {"kind": "logo"}, {"kind": "unknown"}):
        assert plan_cover((assessment(**updates),), event_year=2026)["mode"] == "text"


def test_archive_requires_year_label_and_album_requires_distinct_value() -> None:
    old = assessment(kind="archive_photo", event_year=2025, image_url="https://example.org/old.jpg")
    assert plan_cover((old,), event_year=2026)["mode"] == "text"
    old = old.model_copy(update={"archive_label": "Фото фестиваля 2025 года"})
    another = assessment(kind="current_photo", image_url="https://example.org/new.jpg")
    assert plan_cover((old, another), event_year=2026, allow_album=True)["mode"] == "single_cover"
    distinct = tuple(a.model_copy(update={"distinct_visual_value": True}) for a in (old, another))
    assert plan_cover(distinct, event_year=2026, allow_album=True)["mode"] == "album"
