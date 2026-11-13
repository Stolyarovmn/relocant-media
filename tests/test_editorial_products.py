import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_source_enrichment import _record
from test_source_enrichment import session as source_session

from relocant_media.ai.models import RussianDraft
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.freshness import freshness_decision
from relocant_media.domain.publication_time import publication_interval
from relocant_media.domain.topic_tags import is_topic_footer, topic_tag_spans, topic_tags
from relocant_media.services.editorial_products import (
    build_presentation,
    present_draft,
    rubric_plan,
    validate_presentation,
    verify_live_map,
    verify_stored_presentation,
)
from relocant_media.storage.media_models import RawItemMediaSnapshot

session = source_session


def test_installed_runtime_loads_city_config_from_application_directory(tmp_path, monkeypatch):
    # В Docker модуль находится в site-packages, конфигурация — в /app/cities.
    contents = Path("cities/belgrade/editorial.yaml").read_text(encoding="utf-8")
    target = tmp_path / "cities" / "belgrade"
    target.mkdir(parents=True)
    (target / "editorial.yaml").write_text(contents, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert load_city_profile("belgrade").city_name == "Белград"


def test_belgrade_categories_have_one_public_hashtag():
    profile = load_city_profile("belgrade")
    categories = {category for rubric in profile.rubrics for category in rubric.categories}
    tags = {category: topic_tags(category, presentation_version=4) for category in categories}
    assert {category for category, tag in tags.items() if tag == "#Город"} == {
        "accusations", "city",
    }
    assert tags["events"] == "#События"
    assert tags["culture"] == "#Культура"
    assert tags["transport"] == "#Транспорт"
    assert tags["utilities"] == "#Коммуналка"
    assert topic_tags("unknown", presentation_version=4) == "#Город"
    for tag in tags.values():
        assert is_topic_footer(tag)
        assert topic_tag_spans(tag) == ((0, len(tag)),)


def test_maps_and_rubrics_are_bound_before_review_and_rechecked_from_snapshot(session):
    raw_id, doc = _record(session, "belgrade", "Knez Mihailova 6", "Serbia")
    profile = load_city_profile("belgrade")
    presentation = build_presentation(session, raw_id, (doc,), document_ids=[raw_id],
                                      profile=profile, rubric_id="weekend")
    assert presentation.source_map is not None
    draft = RussianDraft(
        title="Городское событие #Белград",
        body=("Название акции — «#МойГород». Билет #13.\n"
              "Подробности: https://example.org/афиша/#вход.\n\n"
              "#События #Досуг\n#Афиша2026 #event@belgrade"),
        claim_ids=("c1",),
    )
    rendered = present_draft(draft, presentation, category="events")
    assert presentation.version == 4
    assert present_draft(rendered, presentation, category="events") == rendered
    assert rendered.title == "Городское событие Белград"
    assert rendered.body == (
        "Название акции — «МойГород». Билет #13.\n"
        "Подробности: https://example.org/афиша/#вход.\n\n"
        f"Карта из источника: {presentation.source_map['url']}\n\n#События"
    )
    historical = presentation.model_copy(update={"version": 2})
    assert present_draft(rendered, historical, category="events").body.endswith("#События #Досуг")
    version_three = presentation.model_copy(update={"version": 3})
    assert present_draft(draft, version_three, category="events").title == draft.title
    assert present_draft(draft, version_three, category="events").body.endswith(
        f"#Афиша2026 #event@belgrade\n\nКарта из источника: {presentation.source_map['url']}"
        "\n\n#События"
    )
    validate_presentation(
        presentation, profile, (doc,), rendered, rubric_id="weekend", category="events",
    )
    verify_stored_presentation(session, raw_id, (doc,), document_ids=[raw_id],
                              presentation=presentation)
    html = session.get(RawItemMediaSnapshot, raw_id).source_html
    verify_live_map(presentation, doc, html)
    # Динамический footer не меняет карту, но подмена href при прежнем body блокируется.
    verify_live_map(presentation, doc, html + "<footer>Обновление сайта</footer>")
    with pytest.raises(ValueError, match="живом источнике"):
        verify_live_map(presentation, doc, html.replace("query=belgrade", "query=other"))
    with pytest.raises(ValueError, match="живом источнике"):
        verify_live_map(presentation, doc, None)
    # Даже разрешённый провайдер карты не разрешает подмену места в присланном пакете.
    altered = presentation.model_copy(update={"source_map": {
        **presentation.source_map, "url": "https://www.google.com/maps/place/different"}})
    with pytest.raises(ValueError, match="Карта"):
        verify_stored_presentation(session, raw_id, (doc,), document_ids=[raw_id],
                                  presentation=altered)
    with pytest.raises(ValueError, match="Оформление"):
        validate_presentation(
            presentation, profile, (doc,), draft, rubric_id="weekend", category="events",
        )


def test_rubric_calendar_is_reproducible_and_uses_city_day_and_actual_publications():
    profile = load_city_profile("belgrade").model_copy(update={
        "city_id": "tbilisi", "city_name": "Тбилиси", "country_code": "GE",
        "country_name": "Грузия", "timezone": "Asia/Tbilisi"})
    now = datetime(2026, 11, 8, 21, tzinfo=UTC)  # В Тбилиси уже понедельник.
    history = [
        {"rubric_id": "city_changes", "status": "published",
         "published_at": "2026-11-08T20:30:00+00:00"},
        {"rubric_id": "weekend", "status": "published",
         "published_at": "2026-11-08T19:30:00+00:00"},
        {"rubric_id": "weekend", "status": "ready",
         "published_at": "2026-11-08T20:30:00+00:00"},
    ]
    plan = rubric_plan(profile, history, now=now)
    assert json.dumps(plan, sort_keys=True) == json.dumps(
        rubric_plan(profile, history, now=now), sort_keys=True)
    rows = {item["rubric_id"]: item for item in plan["rubrics"]}
    assert rows["city_changes"]["remaining"] == 0
    assert rows["weekend"]["published"] == 0
    assert rows["weekend"]["period_start"] == "2026-11-09T00:00:00+04:00"
    assert plan["targets_are_not_publication_quotas"]


def test_freshness_and_day_precision_follow_other_country_timezone():
    published = datetime(2026, 11, 8, 21, tzinfo=UTC)
    start, end = publication_interval(published, "day", timezone_name="Asia/Tbilisi")
    assert start == datetime(2026, 11, 8, 20, tzinfo=UTC)
    assert end == datetime(2026, 11, 9, 20, tzinfo=UTC)
    # «Завтра» означает10ноября в Тбилиси и9ноября в Белграде.
    now = datetime(2026, 11, 10, 10, tzinfo=UTC)
    args = ("Отключение воды", "Завтра отключат воду в районе.", published, now)
    assert freshness_decision(*args, timezone_name="Asia/Tbilisi").action == "keep"
    assert freshness_decision(*args, timezone_name="Europe/Belgrade").reason == "event_expired"
