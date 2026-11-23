import gzip
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_transport_map import _article, _map_html, _notice
from test_utility_scope import document as power_document
from test_water_outage_scope import document as water_document

from relocant_media.ai.models import EvidenceDocument, RussianDraft
from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI, map_reply_markup
from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.map_links import map_event_id, map_event_url
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.ops.belgrade_digests import morning_entries
from relocant_media.ops.map_backfill import parse_targets, plan
from relocant_media.ops.map_export import (
    Area,
    _power,
    _water,
    _water_aliases,
    encode_map_feed,
    export_map,
    export_map_snapshot,
    read_map_feed,
)
from relocant_media.ops.newsroom_publish import MAP_PATH, _publish_map
from relocant_media.ops.pages_assets import copy_assets
from relocant_media.services.editorial_products import (
    build_presentation,
    present_draft,
    validate_presentation,
    verify_stored_presentation,
)
from relocant_media.services.local_outage_geometry import (
    AddressLocation,
    AreaIndex,
    AreaPolygon,
    address_key,
)
from relocant_media.services.local_street_geometry import load_street_index
from relocant_media.services.native_map import (
    SECRETARIAT_MAP_MODE,
    bind_native_map_draft,
    verify_live_native_map,
)
from relocant_media.services.rich_delivery import prepare_native_transport
from relocant_media.services.road_scope import resolve_secretariat_stretches
from relocant_media.services.transport_map import build_transport_map
from relocant_media.services.utility_scope import extract_power_scopes
from relocant_media.services.water_outage_scope import extract_water_outage_scopes
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import (
    Base,
    EditorialRun,
    Post,
    RawItem,
    SourceRevision,
    Submission,
)

NOW = datetime(2026, 11, 12, 7, tzinfo=UTC)
_LISTED_URL = (
    "https://www.bgsaobracaj.rs/vest/5435/izmena-rezima-saobraca%D1%98a-u-ustanicko%D1%98"
)
_LISTED_ROAD = (
    "Доћи ће до заузећа дела коловоза у ул. Устаничкој , на територији градске општине "
    "Вождовац, у временском периоду од 20. 11. до 25. 11. 2026. године, по фазама:\n\n"
    "Фаза 1 : заузеће десне коловозне траке од ул. Грчића Миленка до ул. Крушевачке "
    "у временском периоду од 20. 11. до 21. 11. 2026. године:\n\n"
    "Фаза 2 : заузеће десне коловозне траке од ул. Јована Суботића до ул. Грчића Миленка "
    "у временском периоду од 22. 11. до 25. 11. 2026. године.\n\n"
    "Мапа заузећа"
)
_SIDEWALK_URL = (
    "https://www.bgsaobracaj.rs/vest/5432/izmena-rezima-saobraca%D1%98a-u-ul-"
    "vla%D1%98kovicevo%D1%98-i-kosovsko%D1%98"
)
_SIDEWALK_BODY = (
    "Због изградње гараже заузет тротоар у ул. Влајковићевој , од Трга Николе Пашића "
    "до ул. Косовске, заузет део тротоара и паркинг места у ул. Косовској, у зони бр. 33-35, "
    "на територији Градске општине Стари град, у временском периоду "
    "14. 11. 2026. до 1. 5. 2027. године.\n\nМапа заузећа"
)
_TRAFFIC_CHANGE_URL = (
    "https://www.bgsaobracaj.rs/vest/5438/izmena-rezima-saobraca%D1%98a-u-ul-dr-velizara-kosanovica"
)
_TRAFFIC_CHANGE_BODY = (
    "Биће измењен режим саобраћаја у ул. Др Велизара Косановића, од ул. Паје Јовановића "
    "до ул. Живка Давидовића, на територији Градске општине Звездара, "
    "у временском периоду од 23. 11. 2026. до 21. 12. 2026. године.\n\nМапа заузећа"
)


def _listed_document(body: str = _LISTED_ROAD) -> EvidenceDocument:
    return EvidenceDocument(
        source_id="bgsaobracaj_notices", url=_LISTED_URL, title="Промена саобраћаја у Устаничкој",
        body=body, published_at=datetime(2026, 11, 19, tzinfo=UTC), published_at_precision="day",
    )


def test_traffic_change_connects_named_crossings_and_binds_the_post_button(monkeypatch):
    document = EvidenceDocument(
        source_id="bgsaobracaj_notices", url=_TRAFFIC_CHANGE_URL,
        title="Измена режима саобраћаја у ул. Др Велизара Косановића", body=_TRAFFIC_CHANGE_BODY,
    )
    streets = load_street_index(Path("cities/belgrade/map/roads.json.gz"))
    source = load_source_file(Path("cities/belgrade/sources/bgsaobracaj_notices.yaml"))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sync_source(session, "belgrade", source)
        capture_collected_batch(session, [
            CollectedItem(**document.model_dump(), external_id="5438", source_html="<article/>"),
        ])
        session.commit()
        observed_at = datetime(2026, 11, 23, 18, tzinfo=UTC)
        (incident,) = export_map(session, observed_at=observed_at, streets=streets).incidents
        (line,) = incident.lines
        assert line.scope == "traffic_change"
        assert line.starts_at == datetime(2026, 11, 22, 23, tzinfo=UTC)
        assert line.ends_at == datetime(2026, 12, 21, 23, tzinfo=UTC)
        assert line.coordinates[0] == (44.7900295, 20.5063241)
        assert line.coordinates[-1] == (44.7921326, 20.5087493)

        profile = load_city_profile("belgrade")
        presentation = build_presentation(session, 1, (document,), document_ids=[1],
                                          profile=profile, rubric_id="city_changes")
        draft = present_draft(
            RussianDraft(title="На Др Велизара Косановича меняют движение",
                         body="Участок от Пае Йовановича до Живка Давидовича.",
                         claim_ids=("street",)),
            presentation, category="transport",
        )
        presentation = bind_native_map_draft(presentation, draft)
        native = presentation.native_map
        assert native is not None
        assert native["point"]["road_phases"][0]["scope"] == line.scope
        verify_stored_presentation(session, 1, (document,), document_ids=[1],
                                   presentation=presentation)
        monkeypatch.setenv("RELOCANT_MAP_TELEGRAM_APP", "https://t.me/relocant_media_belgrade_bot")
        package = SimpleNamespace(presentation=presentation, outcome=SimpleNamespace(draft=draft))
        publisher = SimpleNamespace(bot=SimpleNamespace(send_notice_message=lambda *args: None))
        _, prepared = prepare_native_transport(
            package, publisher, None,
            editorial_text=f"{draft.title}\n\n{draft.body}\n\nИсточник: {document.url}",
        )
        assert prepared.map_url == f"https://t.me/relocant_media_belgrade_bot?startapp={incident.id}"
    engine.dispose()

    timed = document.model_copy(update={
        "body": _TRAFFIC_CHANGE_BODY + " Сваког дана од 8 до 17 часова.",
    })
    assert not resolve_secretariat_stretches(timed, streets)
    # Add another crossing on the connected street. A reachable alternative is ambiguous.
    streets["звездара"]["живка давидовића"].append([
        9999999999, "Живка Давидовића",
        [[9998193542, 44.7920019, 20.508649], [9999999999, 44.7920000, 20.508660]],
    ])
    assert not resolve_secretariat_stretches(document, streets)


def test_listed_road_phases_use_the_same_source_for_map_and_delivery(monkeypatch):
    source = load_source_file(Path("cities/belgrade/sources/bgsaobracaj_notices.yaml"))
    streets = load_street_index(Path("cities/belgrade/map/roads.json.gz"))
    document = _listed_document()
    html = "<article><h2>Промена саобраћаја у Устаничкој</h2></article>"
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sync_source(session, "belgrade", source)
        capture_collected_batch(session, [
            CollectedItem(**document.model_dump(), external_id="5435", source_html=html),
        ])
        session.commit()
        observed_at = datetime(2026, 11, 23, 7, tzinfo=UTC)
        (incident,) = export_map(session, observed_at=observed_at, streets=streets).incidents
        first, second = incident.lines
        switch = datetime(2026, 11, 21, 23, tzinfo=UTC)
        assert first.ends_at == switch == second.starts_at
        assert second.ends_at == datetime(2026, 11, 25, 23, tzinfo=UTC)
        assert first.coordinates[0] == second.coordinates[-1]
        assert first.coordinates != second.coordinates
        assert len(first.coordinates) > 2 and len(second.coordinates) > 2
        assert "Крушевачка" in first.label and "Јована Суботића" in second.label

        profile = load_city_profile("belgrade")
        presentation = build_presentation(session, 1, (document,), document_ids=[1],
                                          profile=profile, rubric_id="city_changes")
        draft = present_draft(
            RussianDraft(title="Работы на Устаничкой", body="Вторая фаза идёт до 25 ноября.",
                         claim_ids=("period",)),
            presentation, category="transport",
        )
        presentation = bind_native_map_draft(presentation, draft)
        native = presentation.native_map
        assert native is not None
        assert native["point"]["mode"] == SECRETARIAT_MAP_MODE
        first_phase, second_phase = native["point"]["road_phases"]
        assert first_phase["ends_on"] == "2026-11-21" and second_phase["starts_on"] == "2026-11-22"
        assert first_phase["segment"]["end"] == "Крушевачка"
        assert first_phase["geometry_sha256"] != second_phase["geometry_sha256"]
        validate_presentation(presentation, profile, (document,), draft,
                              rubric_id="city_changes", category="transport")
        verify_stored_presentation(session, 1, (document,), document_ids=[1],
                                   presentation=presentation)
        altered = presentation.model_copy(update={"native_map": {**native, "point": {
            **native["point"], "road_phases": [
                {**first_phase, "geometry_sha256": "0" * 64}, second_phase,
            ],
        }}})
        with pytest.raises(ValueError, match="road map differs from the exact source snapshot"):
            verify_stored_presentation(session, 1, (document,), document_ids=[1],
                                       presentation=altered)
        verify_live_native_map(native, document, html)
        changed = _listed_document(_LISTED_ROAD.replace("22. 11.", "23. 11."))
        with pytest.raises(ValueError, match="road map changed in live source"):
            verify_live_native_map(native, changed, html)

        monkeypatch.setenv("RELOCANT_MAP_TELEGRAM_APP", "https://t.me/relocant_media_belgrade_bot")
        package = SimpleNamespace(presentation=presentation, outcome=SimpleNamespace(draft=draft))
        publisher = SimpleNamespace(bot=SimpleNamespace(send_notice_message=lambda *args: None))
        _, prepared = prepare_native_transport(
            package, publisher, None,
            editorial_text=f"{draft.title}\n\n{draft.body}\n\nИсточник: {document.url}",
        )
        assert prepared.map_url == f"https://t.me/relocant_media_belgrade_bot?startapp={incident.id}"
        assert prepared.source_preview_url == document.url
        assert prepared.photo is None
    engine.dispose()


def test_incomplete_or_conflicting_listed_road_phases_are_not_mapped():
    streets = load_street_index(Path("cities/belgrade/map/roads.json.gz"))
    for changed in (
        _LISTED_ROAD.replace("од ул. Јована Суботића", "од почетка улице"),
        _LISTED_ROAD.replace("22. 11. до 25. 11.", "22. 11. до 26. 11."),
        _LISTED_ROAD.replace("Фаза 2", "Фаза 1"),
        _LISTED_ROAD.replace("Вождовац", "Стари град"),
        _LISTED_ROAD.replace("коловоза у ул. Устаничкој ,", "коловоза у ул. Устаничкој и Јужна,"),
    ):
        assert resolve_secretariat_stretches(_listed_document(changed), streets) == []


def test_secretariat_sidewalk_exports_only_the_bounded_section_and_preserves_its_kind():
    streets = load_street_index(Path("cities/belgrade/map/roads.json.gz"))
    document = _listed_document(_SIDEWALK_BODY).model_copy(update={
        "url": _SIDEWALK_URL, "title": "Измена режима саобраћаја у Старом граду",
    })
    source = load_source_file(Path("cities/belgrade/sources/bgsaobracaj_notices.yaml"))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sync_source(session, "belgrade", source)
        capture_collected_batch(session, [
            CollectedItem(**document.model_dump(), external_id="5432", source_html="<article/>"),
        ])
        session.commit()
        observed_at = datetime(2026, 11, 23, 7, tzinfo=UTC)
        (incident,) = export_map(session, observed_at=observed_at, streets=streets).incidents
        assert incident.id == map_event_id(_SIDEWALK_URL)
        assert incident.starts_on.isoformat() == "2026-11-14"
        assert incident.ends_on.isoformat() == "2027-05-01"
        (line,) = incident.lines
        assert line.scope == "sidewalk"
        assert line.label == "Тротуар · Влајковићева: Трг Николе Пашића — Косовска"
        assert len(line.coordinates) > 2
        assert line.ends_at == datetime(2027, 5, 1, 22, tzinfo=UTC)
        assert not incident.points

        profile = load_city_profile("belgrade")
        presentation = build_presentation(session, 1, (document,), document_ids=[1],
                                          profile=profile, rubric_id="city_changes")
        assert presentation.native_map is not None
        (phase,) = presentation.native_map["point"]["road_phases"]
        assert phase["scope"] == "sidewalk"
        assert phase["segment"]["start"] == "Трг Николе Пашића"
        assert phase["segment"]["end"] == "Косовска"
        verify_stored_presentation(session, 1, (document,), document_ids=[1],
                                   presentation=presentation)
    engine.dispose()

    for changed in (
        _SIDEWALK_BODY.replace("од Трга Николе Пашића до ул. Косовске", "у зони гараже"),
        _SIDEWALK_BODY.replace("Стари град", "Вождовац"),
        _SIDEWALK_BODY.replace("Због изградње гараже", "Радови се изводе сваке суботе"),
        "Датум објаве 18. 11. 2026. године.\n\n" + _SIDEWALK_BODY,
        _SIDEWALK_BODY.replace("заузет тротоар", "доступан тротоар"),
    ):
        modified = document.model_copy(update={"body": changed})
        assert not resolve_secretariat_stretches(modified, streets)


def test_official_road_phases_have_separate_lines_and_follow_valid_revisions():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    source = load_source_file(Path("cities/belgrade/sources/bgsaobracaj_notices.yaml"))
    streets = load_street_index(Path("cities/belgrade/map/roads.json.gz"))
    today = datetime(2026, 11, 23, 7, tzinfo=UTC)
    url = "https://www.bgsaobracaj.rs/vest/5423/vojvode-sime-popovica"
    body = (
        "Биће обустављен саобраћај у улици Војводе Симе Поповића, "
        "од ул. Новице Церовића до ул. Милана Грола, фаза 2, "
        "у периоду од 14. 11. 2026. до 28. 11. 2026. године и "
        "од ул. Милана Грола до ул. Бана Иваниша, фаза 3. "
        "у периоду од 29. 11. 2026. до 13. 12. 2026. године."
    )
    original = CollectedItem(source.id, "5423", url, "Обустава саобраћаја", body,
                             today - timedelta(days=10), published_at_precision="day")
    with Session(engine) as session:
        sync_source(session, "belgrade", source)
        capture_collected_batch(session, [original])
        item = session.scalar(select(RawItem))
        assert item is not None
        session.add(EditorialRun(raw_item_id=item.id, outcome={}, evidence={},
                                 delivery_status="pending"))
        session.commit()
        (incident,) = export_map(session, observed_at=today, streets=streets).incidents
        assert incident.id == map_event_id(url)
        assert len(incident.lines) == 2
        first, second = incident.lines
        assert first.coordinates[-1] == second.coordinates[0]
        assert first.coordinates != second.coordinates
        assert first.ends_at == second.starts_at == datetime(2026, 11, 28, 23, tzinfo=UTC)
        assert second.ends_at == datetime(2026, 12, 13, 23, tzinfo=UTC)
        assert export_map(session, observed_at=today, streets={}).incidents == ()

        for revised_body, expected in (
            (
                body.replace("13. 12. 2026.", "11. 12. 2026."),
                datetime(2026, 12, 11, 23, tzinfo=UTC),
            ),
            (body.replace("до ул. Бана Иваниша", "до завршетка радова"), None),
        ):
            revision_time = today + timedelta(minutes=1 if expected else 2)
            revised = CollectedItem(source.id, "5423", url, original.title,
                                    revised_body, original.published_at,
                                    published_at_precision="day")
            assert capture_collected_batch(session, [revised]) == (0, 1)
            revision = session.scalar(select(SourceRevision).order_by(SourceRevision.id.desc()))
            assert revision is not None
            revision.observed_at = revision_time
            session.commit()
            assert item.body == body
            updated = export_map(session, observed_at=revision_time, streets=streets).incidents
            if expected is None:
                assert updated == ()
            else:
                assert updated[0].id == incident.id
                assert updated[0].lines[-1].ends_at == expected
    engine.dispose()


def test_backfill_accepts_only_the_exact_owner_receipt_for_a_water_post():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    water = water_document(source_id="city_beograd_news", published_at=NOW)
    with Session(engine) as session:
        sync_source(session, "belgrade", load_source_file(
            Path("cities/belgrade/sources/city_beograd_news.yaml"),
        ))
        capture_collected_batch(session, [
            CollectedItem(**water.model_dump(), external_id="116609"),
        ])
        raw = session.scalar(select(RawItem))
        post = Post(city="belgrade", category="utilities", title="Отключение воды", body="Тест",
                    status="published", confidence=1, telegram_message_id=149, published_at=NOW)
        session.add(post)
        session.flush()
        session.add(EditorialRun(raw_item_id=raw.id, post_id=post.id, delivery_status="sent",
                                 evidence={}, outcome={"_package_sha256": "a" * 64}))
        receipt = Submission(kind="owner_editorial_review", status="published", payload={
            "raw_item_id": raw.id, "post_id": post.id, "owner_decision": "publish",
            "telegram_channel_message_id": 149, "package_sha256": "a" * 64,
        })
        session.add(receipt)
        session.flush()
        feed = export_map(session, observed_at=NOW)
        # Геометрия может быть только контуром, без точек или адресов.
        incident = feed.incidents[0]
        area = Area(label="Квартал", scope="settlement", starts_at=incident.starts_at,
                    ends_at=incident.ends_at,
                    rings=(((44.8, 20.4), (44.8, 20.5), (44.9, 20.5), (44.8, 20.4)),))
        incident = incident.model_copy(update={"areas": (area,)})
        feed = feed.model_copy(update={"incidents": (incident,)})
        targets = {149: incident.id}
        app = "https://t.me/relocant_media_belgrade_bot"
        assert plan(session, targets, feed, app) == {149: f"{app}?startapp={incident.id}"}
        receipt.payload = {**receipt.payload, "package_sha256": "b" * 64}
        session.flush()
        with pytest.raises(ValueError, match="must match"):
            plan(session, targets, feed, app)
    engine.dispose()


def test_official_suspension_revision_removes_stale_map_without_rewriting_reviewed_evidence():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    notice = _notice()
    with Session(engine) as session:
        sync_source(session, "belgrade", load_source_file(
            Path("cities/belgrade/sources/bgprevoz_changes.yaml"),
        ))
        original = CollectedItem(**notice.model_dump(), external_id="bgprevoz-1151",
                                 source_html=_map_html(line=True))
        capture_collected_batch(session, [original])
        item = session.scalar(select(RawItem))
        item.ingested_at = NOW
        session.add(EditorialRun(raw_item_id=item.id, outcome={}, evidence={},
                                 delivery_status="pending"))
        session.commit()
        assert len(export_map(session, observed_at=NOW).incidents) == 1
        cancelled = CollectedItem(**notice.model_copy(update={"body": (
            "Приостановлено. Период на странице: 17/11/2026 - 16/11/2026.\n"
            "Радови су обустављени до даљег."
        )}).model_dump(), external_id="bgprevoz-1151")
        assert capture_collected_batch(session, [cancelled]) == (0, 1)
        revision = session.scalar(select(SourceRevision))
        revision.observed_at = NOW
        session.commit()
        assert item.body == notice.body  # История редакционного ревью неизменна.
        assert export_map(session, observed_at=NOW).incidents == ()
        revision.snapshot_sha256 = "0" * 64
        session.commit()
        assert len(export_map(session, observed_at=NOW).incidents) == 1
    engine.dispose()


def test_map_exports_three_layers_and_archived_button_target(tmp_path):
    database = tmp_path / "editorial database.sqlite3"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    power = power_document(
        "Vračar 9.00 - 11.00 VOJVODE ŠUPLJIKCA: 42, 44–48,\n"
        "Zvezdara 13.00 - 15.00 BRANKA KRSMANOVIĆA: 2,",
    )
    water = water_document(source_id="city_beograd_news", published_at=NOW)
    article, notice = _article(), _notice()
    with Session(engine) as session:
        for source_id in {d.source_id for d in (power, water, article, notice)}:
            sync_source(session, "belgrade", load_source_file(
                Path("cities/belgrade/sources") / f"{source_id}.yaml",
            ))
        capture_collected_batch(session, [
            CollectedItem(**power.model_dump(), external_id="116619"),
            CollectedItem(**water.model_dump(), external_id="116609"),
            CollectedItem(**article.model_dump(), external_id="116589"),
            CollectedItem(**notice.model_dump(), external_id="bgprevoz-1151",
                          source_html=_map_html(line=True)),
        ])
        for item in session.scalars(select(RawItem)):
            item.ingested_at = NOW
        anchor = session.scalar(select(RawItem).where(RawItem.url == article.url))
        native = build_transport_map(session, article)
        assert anchor is not None and native is not None
        post = Post(city="belgrade", category="transport", title="Работы в Сремчице",
                    body="Частные черновые сведения для теста", status="human_review",
                    confidence=1, telegram_message_id=123, published_at=NOW)
        session.add(post)
        session.flush()
        session.add(EditorialRun(
            raw_item_id=anchor.id, post_id=post.id, delivery_status="sent",
            evidence={"private": "Текст предложки"},
            outcome={
                "_package": {"presentation": {"native_map": native}},
                "_rich_delivery": {"state": "sent", "message_ids": [123]},
            },
        ))
        session.commit()
    engine.dispose()

    path = tmp_path / "site/map/incidents.json"
    export_map_snapshot(database, path, NOW.isoformat())
    content = path.read_bytes()
    feed = read_map_feed(content)
    assert "Частные черновые сведения".encode() not in content
    assert "Текст предложки".encode() not in content
    assert {e.kind for e in feed.incidents} == {"power", "water", "road"}
    by_kind = {e.kind: e for e in feed.incidents}
    assert len(by_kind["power"].windows) == 2
    assert [(a.number, a.starts_at.hour, a.ends_at.hour) for a in by_kind["power"].addresses] == [
        ("42", 9, 11), ("2", 13, 15),
    ]
    assert by_kind["water"].starts_at.isoformat() == "2026-11-13T08:30:00+01:00"
    assert by_kind["water"].segments[0].start == "Vidske"
    assert not by_kind["water"].lines
    road = by_kind["road"]
    assert road.id == map_event_id(article.url) == map_event_url(article.url).split("=")[1]
    assert road.source_map_url == notice.url
    assert road.starts_on.isoformat() == "2026-11-10"
    assert road.ends_on.isoformat() == "2026-12-05"
    assert road.lines[0].coordinates[0] == (44.650515, 20.404065)
    assert len(feed.incidents) == 3
    with Session(engine) as session:
        targets = parse_targets(f"123:{road.id}")
        app = "https://t.me/relocant_media_belgrade_bot"
        assert plan(session, targets, feed, app) == {123: f"{app}?startapp={road.id}"}
        with pytest.raises(ValueError, match="must match"):
            plan(session, {123: by_kind["water"].id}, feed, app)
        with pytest.raises(ValueError, match="missing from snapshot"):
            plan(session, {**targets, 124: road.id}, feed, app)
        with pytest.raises(ValueError, match="repeated"):
            parse_targets(f"123:{road.id},123:{road.id}")

    # Настоящие узлы и связность в рабочем индексе, здесь минимальный граф фикстуры.
    road_data = {"version": 1, "municipalities": {"Voždovac": [
        [100, "Краља Бодина", [[11, 44.77, 20.49], [12, 44.78, 20.49], [13, 44.79, 20.49]]],
        [101, "Видска", [[21, 44.77, 20.48], [11, 44.77, 20.49]]],
        [102, "Крушевачка", [[13, 44.79, 20.49], [31, 44.79, 20.50]]],
    ]}}
    streets = tmp_path / "roads.json.gz"
    streets.write_bytes(gzip.compress(json.dumps(road_data, ensure_ascii=False).encode()))
    export_map_snapshot(database, path, NOW.isoformat(), street_index=streets)
    actual = next(e for e in read_map_feed(path.read_bytes()).incidents if e.kind == "water")
    assert actual.lines[0].coordinates == ((44.77, 20.49), (44.78, 20.49), (44.79, 20.49))

    old_active = datetime(2026, 12, 1, 7, tzinfo=UTC)
    export_map_snapshot(database, path, old_active.isoformat())
    ongoing, = read_map_feed(path.read_bytes()).incidents
    assert ongoing.id == road.id and ongoing.ends_at > old_active
    for completed in by_kind.values():
        limit = completed.ends_at + timedelta(days=14)
        export_map_snapshot(database, path, limit.isoformat())
        assert completed.id in {item.id for item in read_map_feed(path.read_bytes()).incidents}
        export_map_snapshot(database, path, (limit + timedelta(seconds=1)).isoformat())
        assert completed.id not in {item.id for item in read_map_feed(path.read_bytes()).incidents}

    engine = create_engine(f"sqlite:///{database}")
    with Session(engine) as session:
        changed = session.scalar(select(RawItem).where(RawItem.url == notice.url))
        changed.body = changed.body.replace("05.12.2026", "07.12.2026")
        session.commit()
    engine.dispose()
    export_map_snapshot(database, path, (NOW + timedelta(days=14)).isoformat())
    archived = next(item for item in read_map_feed(path.read_bytes()).incidents
                    if item.id == road.id)
    assert archived.id == road.id and archived.archived
    assert archived.lines == road.lines
    assert archived.ends_at is None and archived.published_at == NOW
    export_map_snapshot(database, path, (NOW + timedelta(days=14, seconds=1)).isoformat())
    assert road.id not in {item.id for item in read_map_feed(path.read_bytes()).incidents}


def test_public_map_schema_rejects_unexpected_private_fields():
    with pytest.raises(ValidationError, match="Extra inputs"):
        read_map_feed(b'{"version":1,"incidents":[],"bot_token":"test"}')


@pytest.mark.parametrize("bulleted", [False, True])
def test_water_copy_remains_a_valid_link_and_does_not_duplicate_the_morning_block(bulleted):
    base = "https://www.beograd.rs/lat/"
    narrow_url = (base + "servisne-informacije/vodovod-i-kanalizacija/"
                  "a116752/Planirani-radovi-na-vodovodnoj-mrezi.html")
    full_url = base + "beoinfo-vesti/a116747/Radovi-na-vodovodnoj-mrezi-u-utorak.html"
    savski = (
        "Zbog planiranih radova na vodovodnoj mreži u opštini Savski venac, "
        "u utorak, 24. novembra, u periodu od 8 do 15 sati, "
        "bez vode će ostati potrošači u bloku oivičenom Fabrisovom, Zvečanskom, "
        "Dr Milutina Ivkovića i Oblakovskom ulicom."
    )
    vozdovac = (
        "U opštini Voždovac, u utorak, 24. novembra, u periodu od 8 do 16 sati, "
        "bez vode će ostati potrošači u ulicama Katarine Ivanović i Rastka Petrovića."
    )
    service = savski
    if bulleted:
        service = (
            f"{savski}\n\nVOŽDOVAC\n\n"
            "U opštini Voždovac, u utorak, 24. novembra, u periodu od 8 do 16 sati, "
            "bez vode će ostati potrošači u sledećim ulicama:\n\n"
            "Katarine Ivanović\n\nRastka Petrovića"
        )
    now = datetime.fromisoformat("2026-11-24T07:30:00+01:00")
    source = load_source_file(Path("cities/belgrade/sources/city_beograd_news.yaml"))
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sync_source(session, "belgrade", source)
        items = [
            CollectedItem(
                source_id=source.id, url=url, external_id=external_id,
                title="Planirani radovi na vodovodnoj mreži", body=body,
                published_at=datetime(2026, 11, 22, 7, tzinfo=UTC),
                published_at_precision="day",
            ) for url, external_id, body in (
                (narrow_url, "a116752", service), (full_url, "a116747", f"{savski}\n\n{vozdovac}"),
            )
        ]
        capture_collected_batch(session, items)
        session.commit()
        feed = read_map_feed(encode_map_feed(export_map(session, observed_at=now)))
        narrow_id, full_id = map_event_id(narrow_url), map_event_id(full_url)
        assert feed.water_aliases == {narrow_id: full_id}
        assert {notice.id for notice in feed.incidents} == {narrow_id, full_id}
        narrow, full = (next(notice for notice in feed.incidents if notice.id == event_id)
                        for event_id in (narrow_id, full_id))
        municipalities = {scope.municipality for scope in full.water_districts}
        assert municipalities == {"Voždovac", "Savski venac"}
        if bulleted:
            assert set(narrow.water_districts) == set(full.water_districts)
            assert _water_aliases((narrow, full), frozenset({narrow_id})) == {full_id: narrow_id}
            distinct_map = narrow.model_copy(update={"source_map_url": narrow_url})
            distinct_full = full.model_copy(update={"source_map_url": full_url})
            assert not _water_aliases((distinct_map, distinct_full))
        changed = narrow.water_districts[0].model_copy(
            update={"ends_at": narrow.water_districts[0].ends_at + timedelta(minutes=30)},
        )
        assert not _water_aliases((narrow.model_copy(update={"water_districts": (changed,)}), full))
        (entry,) = morning_entries(session, now, None)
        assert entry.map_id == full_id
        assert any("08:00–15:00" in row for row in entry.address_rows)
        assert any("08:00–16:00" in row for row in entry.address_rows)
    engine.dispose()


def test_power_uses_building_shapes_and_resolves_only_existing_numbers_on_the_same_side():
    evidence = power_document("Vračar 9.00 - 11.00 PRVA ULICA: 2–6, 11,")
    polygon = AreaPolygon(
        (((44.79, 20.45), (44.792, 20.45), (44.792, 20.451), (44.79, 20.45)),),
        "https://www.openstreetmap.org/way/100",
    )
    locations = [
        AddressLocation("Prva ulica", number, "Vračar", (44.791, 20.4503), buildings, "")
        for number, buildings in [("2", (polygon,)), ("3", ()), ("6", ())]
    ]
    index = AreaIndex({
        address_key(value.street, value.number, value.municipality): value for value in locations
    }, {})
    event = _power(evidence, extract_power_scopes(evidence), None, None, index)
    assert {address.number for address in event.addresses} == {"2", "6", "11"}
    assert [pin.address.number for pin in event.points] == ["6"]
    assert [area.address.number for area in event.areas] == ["2"]
    assert event.areas[0].ends_at.hour == 11
    assert event.ranges[0].number == "2–6"


def test_whole_microdistrict_removes_only_covered_geometry_in_the_same_window():
    polygon = AreaPolygon(
        (((44.79, 20.40), (44.79, 20.42), (44.82, 20.42), (44.82, 20.40),
          (44.79, 20.40)),),
        "https://www.openstreetmap.org/relation/123",
    )
    locations = [
        AddressLocation("Lepa", number, "Novi Beograd", position, (), "")
        for number, position in [("4", (44.80, 20.41)), ("6", (44.81, 20.43))]
    ]
    index = AreaIndex({
        address_key(item.street, item.number, item.municipality): item for item in locations
    }, {("нови београд", "мз ледине"): (polygon,)})
    power = power_document("\n".join([
        "Novi Beograd 9.00 - 11.00 Celo mikronaselje Ledine, Lepa: 4, 6, 8,",
        "Novi Beograd 11.00 - 13.00 Lepa: 4,",
    ]))
    water = water_document(body="\n".join([
        "U opštini Novi Beograd, 13. novembra 2026. godine, od 09:00 do 11:00, "
        "bez vode će ostati potrošači u celom mikronaselju Ledine i u ulici Lepa, "
        "brojevi 4, 6 i 8.",
        "U opštini Novi Beograd, 13. novembra 2026. godine, od 11:00 do 13:00, "
        "bez vode će ostati potrošači u ulici Lepa, broj 4.",
    ]))
    events = (
        _power(power, extract_power_scopes(power), None, None, index),
        _water(water, None, None, {}, index, extract_water_outage_scopes(water)),
    )
    for event in events:
        assert [area.rings for area in event.areas] == [polygon.rings]
        assert [place.name for place in event.settlements] == ["Ledine"]
        assert {window.starts_at.hour for window in event.windows} == {9, 11}
        assert {(address.number, address.starts_at.hour) for address in event.addresses} == {
            ("6", 9), ("8", 9), ("4", 11),
        }
        assert {(pin.address.number, pin.address.starts_at.hour) for pin in event.points} == {
            ("6", 9), ("4", 11),
        }


@pytest.mark.asyncio
async def test_map_button_repair_updates_only_markup_and_handles_idempotent_retry():
    url = map_event_url(_article().url, telegram_app="https://t.me/relocant_media_belgrade_bot")
    markup = map_reply_markup(url)
    receipt = {"message_id": 96, "chat": {"id": -1001}, "reply_markup": markup}
    replies = iter([
        httpx.Response(200, json={"ok": True, "result": receipt}),
        httpx.Response(400, json={
            "ok": False, "description": "Bad Request: message is not modified",
        }),
        httpx.Response(200, json={"ok": True, "result": {**receipt, "message_id": 97}}),
    ])

    def handler(request):
        assert request.url.path.endswith("/editMessageReplyMarkup")
        assert json.loads(request.content) == {
            "chat_id": -1001, "message_id": 96, "reply_markup": markup,
        }
        return next(replies)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("test-token", client)
        assert await bot.edit_map_button(-1001, 96, url)
        assert not await bot.edit_map_button(-1001, 96, url)
        with pytest.raises(TelegramAPIError, match="does not match"):
            await bot.edit_map_button(-1001, 96, url)


def test_failed_runtime_keeps_last_map_and_does_not_replace_it(tmp_path):
    path = tmp_path / "map/incidents.json"
    previous = {"version": 1, "generated_at": NOW.isoformat(), "incidents": []}

    def handler(request):
        assert request.method == "GET" and request.url.path == "/" + MAP_PATH
        if request.headers["accept"] == "application/vnd.github.object+json":
            return httpx.Response(200, json={"sha": "a" * 40})
        assert request.headers["accept"] == "application/vnd.github.raw+json"
        return httpx.Response(200, json=previous)

    with httpx.Client(base_url="https://fake.invalid", transport=httpx.MockTransport(handler)) as c:
        _publish_map(c, path)
    assert read_map_feed(path.read_bytes()).generated_at == NOW


def test_pages_assembly_preserves_the_verified_incident_feed(tmp_path):
    web = tmp_path / "web/newsroom/map"
    web.mkdir(parents=True)
    (web / "index.html").write_text("Карта", encoding="utf-8")
    city = tmp_path / "cities/belgrade/map"
    city.mkdir(parents=True)
    (city / "addresses.json.gz").write_bytes(gzip.compress(b'{"version":1,"addresses":[]}'))
    site = tmp_path / "site"
    incident = site / "map/incidents.json"
    incident.parent.mkdir(parents=True)
    incident.write_text('{"version":1,"incidents":[]}', encoding="utf-8")
    copy_assets(tmp_path, site)
    assert (site / "map/index.html").read_text(encoding="utf-8") == "Карта"
    assert json.loads((site / "map/addresses.json").read_bytes())["version"] == 1
    assert gzip.decompress((site / "map/addresses.json.gz").read_bytes()) == (
        site / "map/addresses.json"
    ).read_bytes()
    assert json.loads(incident.read_bytes())["version"] == 1


def test_pages_version_changes_with_a_map_module_but_not_with_new_notices(tmp_path):
    source = tmp_path / "web/newsroom/map"
    source.mkdir(parents=True)
    (source / "index.html").write_bytes(b'<script src="app.js?v=__MAP_REVISION__"></script>')
    (source / "app.js").write_bytes(b'import "./layer-views.js?v=__MAP_REVISION__";')
    (source / "offline-worker.js").write_bytes(
        b'const shell = ["./app.js?v=__MAP_REVISION__", "./layer-views.js?v=__MAP_REVISION__"];'
    )
    layer = source / "layer-views.js"
    layer.write_bytes(b"export const cellSize = 68;")
    city = tmp_path / "cities/belgrade/map"
    city.mkdir(parents=True)
    (city / "addresses.json.gz").write_bytes(gzip.compress(b"{}"))
    site = tmp_path / "site"

    def build() -> bytes:
        copy_assets(tmp_path, site)
        destination = site / "map"
        html = (destination / "index.html").read_bytes()
        version = re.search(rb"app\.js\?v=([a-f0-9]{16})", html)
        assert version is not None
        suffix = b"?v=" + version[1]
        app = (destination / "app.js").read_bytes()
        worker = (destination / "offline-worker.js").read_bytes()
        assert b"./layer-views.js" + suffix in app
        assert b"./app.js" + suffix in worker
        assert b"./layer-views.js" + suffix in worker
        assert b"__MAP_REVISION__" not in html + app + worker
        return suffix

    before = build()
    feed = site / "map/incidents.json"
    feed.write_bytes(b'{"version":1,"incidents":[]}')
    assert build() == before
    layer.write_bytes(b"export const cellSize = 76;")
    assert build() != before
    assert feed.read_bytes() == b'{"version":1,"incidents":[]}'
    assert b"?v=__MAP_REVISION__" in (source / "index.html").read_bytes()


@pytest.mark.parametrize(("subject", "title"), [
    ("током извођења радова у улици Јурија Гагарина", "Дорожные работы · Јурија Гагарина"),
    ("током извођења радова на уређењу окретнице „Сремчица“",
     "Работы на разворотной площадке · „Сремчица“"),
    ("бр. 76", "Изменения на маршруте №76"),
])
def test_unpublished_road_cards_put_the_reason_and_place_in_the_heading(subject, title):
    notice = _notice().model_copy(update={
        "title": f"Информација о промени режима рада линија јавног превоза {subject}",
    })
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        sync_source(session, "belgrade", load_source_file(
            Path("cities/belgrade/sources/bgprevoz_changes.yaml"),
        ))
        capture_collected_batch(session, [
            CollectedItem(**notice.model_dump(), external_id="bgprevoz-1151"),
        ])
        (incident,) = export_map(session, observed_at=NOW).incidents
        assert incident.title == notice.title
        assert incident.display_title == title
        assert incident.id == map_event_id(notice.url)
        assert incident.source_url == notice.url
        assert incident.ends_on.isoformat() == "2026-12-05"
        assert not incident.lines
    engine.dispose()


def test_water_map_title_covers_both_parsed_districts_and_keeps_published_title():
    notice = water_document(body=(
        "U opštini Savski venac, u petak, 13. novembra 2026. godine, od 8 do 15 sati, "
        "bez vode će ostati potrošači u ulicama Zvečanska i Oblakovska.\n\n"
        "U opštini Voždovac, u petak, 13. novembra 2026. godine, od 8 do 16 sati, "
        "bez vode će ostati potrošači u ulicama Rastka Petrovića i Katarine Ivanović."
    ))
    scopes = extract_water_outage_scopes(notice)

    fallback = _water(notice, None, None, {}, None, scopes)
    published = _water(notice, None, "Плановые отключения воды в двух районах", {}, None, scopes)

    assert fallback.title == notice.title
    assert fallback.display_title == "Отключение воды · Savski venac, Voždovac"
    assert published.title == "Плановые отключения воды в двух районах"
    assert published.display_title is None
    assert fallback.id == published.id == map_event_id(notice.url)
    assert [window.ends_at.hour for window in fallback.windows] == [15, 16]
