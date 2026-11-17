import pytest

from relocant_media.domain.editorial_products import EditorialPresentation
from relocant_media.domain.event_map_policy import event_map_review_reason


def presentation(version=5, **maps):
    return EditorialPresentation(version=version, profile_sha256="a" * 64,
        rubric_id="weekend", rubric_name="Планы", illustration={}, **maps)


@pytest.mark.parametrize("category,title,body", [
    ("events", "Arab Strap выступят в Zappa Baza", "Когда: 24 ноября.\nПлощадка: Zappa Baza."),
    ("culture", "В галерее открылась выставка", "Посетителей ждут сегодня."),
    ("events", "Вечер музыки", "Где: Dom omladine Beograda"),
])
def test_physical_event_requires_verified_mini_app_place(category, title, body):
    assert event_map_review_reason(presentation(), category, title, body) == (
        "event_place_requires_verification")
    # Ссылка на стороннюю карту не заменяет карточку события в Mini App.
    assert event_map_review_reason(presentation(source_map={"url": "https://example.org"}),
        category, title, body) == "event_place_requires_verification"
    assert event_map_review_reason(presentation(event_place={"id": "verified"}),
        category, title, body) is None
    assert event_map_review_reason(presentation(native_map={"id": "verified"}),
        category, title, body) is None


def test_historical_hashes_remain_compatible_but_pending_delivery_is_checked():
    assert event_map_review_reason(presentation(4), "events", "Концерт", "Анонс") is None
    assert event_map_review_reason(presentation(4), "events", "Концерт", "Анонс",
        current_delivery=True) == "event_place_requires_verification"
    assert event_map_review_reason(None, "events", "Концерт", "Анонс",
        current_delivery=True) == "event_place_requires_verification"
    assert event_map_review_reason(presentation(), "transport", "Концерт", "Анонс") is None
    assert event_map_review_reason(presentation(), "culture", "Издан сборник", "Книги") is None
    assert event_map_review_reason(presentation(), "events", "Музыкальный вечер",
        "В Zappa Baza") == "event_place_requires_verification"
    assert event_map_review_reason(presentation(), "events", "Онлайн-концерт",
        "Формат: полностью онлайн.") is None
