from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from relocant_media.services import shadow_duplicates
from relocant_media.services.shadow_duplicates import build_shadow_duplicate_report
from relocant_media.storage.models import Base, RawItem, Source


def test_shadow_duplicate_report_is_read_only_and_non_authoritative(monkeypatch) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 21, 10, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all(
            [
                Source(
                    id="city",
                    city="belgrade",
                    name="City",
                    url="https://city.example/",
                    source_type="official_news",
                    trust=Decimal("0.900"),
                    ownership="official",
                    auto_publish=True,
                    enabled=True,
                ),
                Source(
                    id="transport",
                    city="belgrade",
                    name="Transport",
                    url="https://transport.example/",
                    source_type="official_news",
                    trust=Decimal("0.900"),
                    ownership="official",
                    auto_publish=True,
                    enabled=True,
                ),
            ]
        )
        session.flush()
        session.add_all(
            [
                RawItem(
                    source_id="city",
                    external_id="1",
                    url="https://city.example/1",
                    title="Затварање улице Кнеза Милоша у суботу",
                    body="Улица Кнеза Милоша биће затворена 24. октобра од 8 до 18 часова.",
                    published_at=now,
                    ingested_at=now,
                ),
                RawItem(
                    source_id="transport",
                    external_id="2",
                    url="https://transport.example/2",
                    title="Zatvaranje ulice Kneza Milosa u subotu",
                    body="Ulica Kneza Milosa bice zatvorena 24. oktobra od 8 do 18 casova.",
                    published_at=now + timedelta(minutes=15),
                    ingested_at=now + timedelta(minutes=15),
                ),
            ]
        )
        session.commit()
        before = session.scalar(select(func.count()).select_from(RawItem))

        report = build_shadow_duplicate_report(session, city="belgrade")
        observed = []

        def fake_jev(pairs, *, city):
            observed.append((pairs, city))
            return {"status": "ok", "decision_effect": "none", "signals": []}

        monkeypatch.setattr(shadow_duplicates, "jev_enabled", lambda: True)
        monkeypatch.setattr(shadow_duplicates, "duplicate_shadow", fake_jev)
        enhanced = build_shadow_duplicate_report(session, city="belgrade")
        after = session.scalar(select(func.count()).select_from(RawItem))

    assert before == after == 2
    assert report["mode"] == "shadow_only"
    assert report["authoritative"] is False
    assert report["decision_effect"] == "none"
    assert report["scorer"] == {
        "id": "lexical_semantic",
        "version": "1",
        "embedding_model": None,
        "embedding_dimension": None,
    }
    assert len(report["signals"]) == 1
    assert report["signals"][0]["left_raw_item_id"] == 1
    assert report["signals"][0]["right_raw_item_id"] == 2
    assert report["signals"][0]["scorer"] == report["scorer"]

    assert enhanced["signals"] == report["signals"]
    assert enhanced["jev_shadow"]["decision_effect"] == "none"
    assert observed[0][1] == "belgrade"
    assert len(observed[0][0]) == 1
    assert observed[0][0][0]["left_title"] == "Затварање улице Кнеза Милоша у суботу"
