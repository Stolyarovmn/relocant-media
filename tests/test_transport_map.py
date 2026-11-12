import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument, RussianDraft
from relocant_media.collectors.base import RawItem
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.ops.newsroom_triage import triage_queue
from relocant_media.services.editorial_products import (
    build_presentation,
    present_draft,
    validate_presentation,
    verify_stored_presentation,
)
from relocant_media.services.native_map import bind_native_map_draft
from relocant_media.services.transport_map import (
    build_transport_map,
    matches_transport_notice,
    transport_point,
)
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import Base, EditorialRun

ARTICLE = "https://www.beograd.rs/lat/beoinfo-vesti/a116589/Izmena.html"
NOTICE = "https://www.bgprevoz.rs/linije/aktuelne-izmene/1151"
NEWS = (
    "Od 10. novembra do 5. decembra 2026. godine zbog radova na okretnici „Sremčica”.\n"
    "Linije broj 511 i 511N. Linije broj 5538 i 860Š."
)
AUTHORITY = (
    "Период измене: 10.11.2026–05.12.2026.\n"
    "Током радова на окретници „Сремчица”. Линије бр. 511 и 511Н. Линије бр. 5538 и 860Ш."
)


def _map_html(*, notice_id=1151, latitude="44.650515", line=False):
    coordinates = [{"lat": 44.650515, "lng": 20.404065}, {"lat": 44.651, "lng": 20.407}]
    notice = {
        "id": notice_id, "mapa_izmene": 1, "lokacija_sirina": latitude,
        "lokacija_duzina": "20.404065",
        "zona_radova": json.dumps({"coordinates": coordinates}) if line else None,
    }
    return f'<div id="gmap-r"></div><script>const izmena = {json.dumps(notice)};\n</script>'


def _article():
    return EvidenceDocument(source_id="city_beograd_news", url=ARTICLE, title="Radovi",
                            body=NEWS, published_at=datetime(2026, 11, 9, tzinfo=UTC))


def _notice():
    return EvidenceDocument(source_id="bgprevoz_changes", url=NOTICE, title="Измена саобраћаја",
                            body=AUTHORITY)


@pytest.mark.parametrize("change", [
    ("5538", "538"), ("10.11.2026", "11.11.2026"), ("Сремчица", "Жарково"),
])
def test_map_requires_exact_period_and_routes_and_shared_location(change):
    article, notice = _article(), _notice()
    assert matches_transport_notice(article, notice)
    mismatched = notice.model_copy(update={"body": notice.body.replace(*change)})
    assert not matches_transport_notice(article, mismatched)
    assert transport_point(article, mismatched, _map_html()) is None


def test_closure_can_match_by_exact_stop_without_route_numbers():
    article = _article().model_copy(update={"body": (
        "Od 12. do 30. novembra 2026. kod stajališta KBC „Bežanijska kosa“ (#1197)."
    )})
    notice = _notice().model_copy(update={"body": (
        "Период измене: 12.11.2026–30.11.2026.\n"
        "Зона аутобуског стајалишта КБЦ „Бежанијска коса“ (#1197)."
    )})
    assert matches_transport_notice(article, notice)
    changed = notice.model_copy(update={"body": notice.body.replace("1197", "1196")})
    assert not matches_transport_notice(article, changed)


@pytest.mark.parametrize("line", [False, True])
def test_transport_map_is_bound_to_snapshot_and_reviewed_html(line):
    article, notice = _article(), _notice()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        for document in (article, notice):
            sync_source(session, "belgrade", load_source_file(
                Path("cities/belgrade/sources") / f"{document.source_id}.yaml",
            ))
        capture_collected_batch(session, [
            RawItem(**article.model_dump(), external_id="116589"),
            RawItem(**notice.model_dump(), external_id="bgprevoz-1151",
                    source_html=_map_html(line=line)),
        ])
        session.commit()
        triage_queue(engine, now=datetime(2026, 11, 10, tzinfo=UTC))
        assert session.get(EditorialRun, 2) is None
        profile = load_city_profile("belgrade")
        presentation = build_presentation(session, 1, (article,), document_ids=[1],
                                          profile=profile, rubric_id="city_changes")
        draft = present_draft(RussianDraft(title="В Сремчице изменились маршруты",
                                           body="Работы идут до 5 декабря 2026 года.",
                                           claim_ids=("period",)), presentation)
        presentation = bind_native_map_draft(presentation, draft)
        native = presentation.native_map
        assert native is not None
        assert native["point"]["source_url"] == ARTICLE
        assert native["point"]["notice_url"] == NOTICE
        assert ("image_sha256" in native["point"]) == line
        assert native["message_html"].startswith(
            '<figure><img src="tg://photo?id=notice_cover"/></figure><h2>'
        )
        assert "<tg-map" not in native["message_html"]
        assert "id=transport_map" not in native["message_html"]
        assert f'href="{ARTICLE}">Источник' in native["message_html"]
        validate_presentation(presentation, profile, (article,), draft, rubric_id="city_changes")
        verify_stored_presentation(session, 1, (article,), document_ids=[1],
                                   presentation=presentation)
        # Сохранённые до обновления пакеты пересобираются прежним способом.
        historical = presentation.model_copy(update={"native_map": {
            key: native[key] for key in ("point", "source_raw_item_id")
        }})
        historical = bind_native_map_draft(historical, draft)
        assert "<tg-map" in historical.native_map["message_html"]
        validate_presentation(historical, profile, (article,), draft, rubric_id="city_changes")
        changed_html = _map_html(line=line, latitude="44.640515")
        assert transport_point(article, notice, changed_html) != native["point"]

        other_notice = notice.model_copy(update={"url": NOTICE.replace("1151", "1152")})
        capture_collected_batch(session, [RawItem(**other_notice.model_dump(),
            external_id="bgprevoz-1152", source_html=_map_html(notice_id=1152, line=line))])
        assert build_transport_map(session, article) is None
        with pytest.raises(ValueError, match="saved official notice"):
            verify_stored_presentation(session, 1, (article,), document_ids=[1],
                                       presentation=presentation)
    engine.dispose()
