import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_transport_map import _article, _map_html, _notice
from test_utility_scope import document as power_document
from test_water_outage_scope import document as water_document

from relocant_media.collectors.base import RawItem as CollectedItem
from relocant_media.domain.map_links import map_event_id, map_event_url
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.ops.map_export import export_map_snapshot, read_map_feed
from relocant_media.ops.newsroom_publish import MAP_PATH, _publish_map
from relocant_media.ops.pages_assets import copy_assets
from relocant_media.services.transport_map import build_transport_map
from relocant_media.sources.registry import load_source_file
from relocant_media.storage.models import Base, EditorialRun, Post, RawItem

NOW = datetime(2026, 11, 12, 7, tzinfo=UTC)


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
                    body="Частные черновые сведения для теста", status="published",
                    confidence=1, telegram_message_id=123, published_at=NOW)
        session.add(post)
        session.flush()
        session.add(EditorialRun(
            raw_item_id=anchor.id, post_id=post.id, delivery_status="sent",
            evidence={"private": "Текст предложки"},
            outcome={"_package": {"presentation": {"native_map": native}}},
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

    export_map_snapshot(database, path, "2027-01-12T07:00:00+00:00")
    assert [e.id for e in read_map_feed(path.read_bytes()).incidents] == [road.id]

    engine = create_engine(f"sqlite:///{database}")
    with Session(engine) as session:
        changed = session.scalar(select(RawItem).where(RawItem.url == notice.url))
        changed.body = changed.body.replace("05.12.2026", "07.12.2026")
        session.commit()
    engine.dispose()
    export_map_snapshot(database, path, "2027-01-12T07:00:00+00:00")
    archived, = read_map_feed(path.read_bytes()).incidents
    assert archived.id == road.id and archived.archived
    assert archived.lines == road.lines


def test_public_map_schema_rejects_unexpected_private_fields():
    with pytest.raises(ValidationError, match="Extra inputs"):
        read_map_feed(b'{"version":1,"incidents":[],"bot_token":"test"}')


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
