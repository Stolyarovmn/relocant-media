from datetime import UTC, datetime, timedelta

from relocant_media.domain.dedup import (
    EventCandidate,
    canonicalize_url,
    cluster_events,
    content_sha256,
    same_event,
)

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def candidate(
    item_id: int,
    source: str,
    *,
    url: str,
    title: str,
    body: str,
    published_at: datetime | None = NOW,
) -> EventCandidate:
    return EventCandidate(
        raw_item_id=item_id,
        source_id=source,
        url=url,
        title=title,
        body=body,
        published_at=published_at,
    )


def test_canonicalize_url_removes_tracking_and_fragment() -> None:
    assert canonicalize_url(
        "HTTPS://Example.COM/news/?utm_source=tg&b=2&a=1#comments"
    ) == "https://example.com/news?a=1&b=2"


def test_exact_content_is_duplicate_even_when_urls_differ() -> None:
    left = candidate(1, "a", url="https://a.test/1", title="Новая линия", body="Текст")
    right = candidate(2, "a", url="https://a.test/2", title="Новая линия", body="Текст")
    assert content_sha256(left.title, left.body) == content_sha256(right.title, right.body)
    assert same_event(left, right)


def test_similar_titles_cluster_only_across_sources_and_inside_window() -> None:
    left = candidate(
        1,
        "city",
        url="https://city.test/1",
        title="Изменение движения трамваев на улице Краља Милана",
        body="Официальное сообщение",
    )
    other_source = candidate(
        2,
        "media",
        url="https://media.test/2",
        title="Изменение движения трамваев на улице Краља Милана сегодня",
        body="Пересказ сообщения",
        published_at=NOW + timedelta(hours=3),
    )
    same_source = candidate(
        3,
        "city",
        url="https://city.test/3",
        title="Изменение движения трамваев на улице Краља Милана сегодня",
        body="Другое сообщение",
        published_at=NOW + timedelta(hours=3),
    )
    late = candidate(
        4,
        "media-2",
        url="https://media.test/4",
        title="Изменение движения трамваев на улице Краља Милана сегодня",
        body="Позднее сообщение",
        published_at=NOW + timedelta(days=3),
    )

    assert same_event(left, other_source)
    assert not same_event(left, same_source)
    assert not same_event(left, late)


def test_same_source_transport_reprint_requires_matching_event_anchors() -> None:
    published = datetime(2026, 10, 21, 12, tzinfo=UTC)
    service_body = "\n".join(
        (
            "Tokom izvođenja radova na redovnom održavanju u zoni raskrsnice ulica "
            "Gandijeve, Egipatske i Vojvođanske, 23. oktobra doći će do promena u "
            "radu linija javnog prevoza.",
            "Radovi podrazumevaju zatvaranje desne kolovozne trake Egipatske i "
            "Vojvođanske, gledano u smeru ka Zemunu, na delu od zone raskrsnice sa "
            "Nehruovom ulicom do skretanja ka objektu KIT komerc.",
            "Vozila sa linija 45, 71, 601, 601N i 603N će u oba smera saobraćati "
            "levom kolovoznom trakom, koja će biti dvosmerna za saobraćaj.",
            "Vozila sa linije 82 će u smeru ka Bloku 44 saobraćati preko Gandijeve, "
            "Vojvođanske i Nehruove.",
            "Stajalište „Crkva Svetog Georgija” (#1136) privremeno se izmešta oko "
            "100 metara, dok se stajalište „Gandijeva” (#1194) u smeru ka Zemunu "
            "privremeno ukida.",
        )
    )
    news_body = "\n".join(
        (
            "Tokom izvođenja radova na redovnom održavanju u zoni raskrsnice ulica "
            "Gandijeve, Egipatske i Vojvođanske, 23. oktobra doći će do promena u "
            "radu javnog prevoza, najavljeno je iz Sekretarijata za javni prevoz.",
            "Radovi podrazumevaju zatvaranje desne kolovozne trake Egipatske i "
            "Vojvođanske, gledano u smeru ka Zemunu, na delu od zone raskrsnice sa "
            "Nehruovom ulicom do skretanja ka objektu KIT komerc.",
            "Vozila sa linija 45, 71, 601, 601N i 603N će u oba smera saobraćati "
            "levom kolovoznom trakom, koja će biti dvosmerna za saobraćaj.",
            "Vozila sa linije 82 će u smeru ka Bloku 44 saobraćati preko Gandijeve, "
            "Vojvođanske i Nehruove.",
            "Stajalište „Crkva Svetog Georgija” (#1136) privremeno se izmešta oko "
            "100 metara, dok se stajalište „Gandijeva” (#1194) u smeru ka Zemunu "
            "privremeno ukida.",
        )
    )
    service = candidate(
        356,
        "city_beograd_news",
        url=(
            "https://www.beograd.rs/lat/servisne-informacije/saobracaj-i-prevoz/"
            "a116344/Radovi-u-zoni-raskrsnice-ulica-Gandijeva-Egipatska-i-"
            "Vojvodjanska.html"
        ),
        title=(
            "SAOBRAĆAJ I PREVOZ: Radovi u zoni raskrsnice ulica Gandijeva, "
            "Egipatska i Vojvođanska"
        ),
        body=service_body,
        published_at=published,
    )
    news = candidate(
        352,
        "city_beograd_news",
        url=(
            "https://www.beograd.rs/lat/beoinfo-vesti/a116340/"
            "Izmena-javnog-prevoza-zbog-radova-u-zoni-raskrsnice-ulica-"
            "Gandijeve-Egipatske-i-Vojvodjanske.html"
        ),
        title=(
            "Izmena javnog prevoza zbog radova u zoni raskrsnice ulica "
            "Gandijeve, Egipatske i Vojvođanske"
        ),
        body=news_body,
        published_at=published + timedelta(hours=3),
    )

    assert same_event(service, news)

    changed_date = candidate(
        400,
        news.source_id,
        url="https://www.beograd.rs/lat/beoinfo-vesti/a116400/changed-date.html",
        title=news.title,
        body=news_body.replace("23. oktobra", "24. oktobra"),
        published_at=news.published_at,
    )
    changed_route = candidate(
        401,
        news.source_id,
        url="https://www.beograd.rs/lat/beoinfo-vesti/a116401/changed-route.html",
        title=news.title,
        body=news_body.replace("603N", "604N"),
        published_at=news.published_at,
    )
    changed_stop = candidate(
        402,
        news.source_id,
        url="https://www.beograd.rs/lat/beoinfo-vesti/a116402/changed-stop.html",
        title=news.title,
        body=news_body.replace("#1194", "#1195"),
        published_at=news.published_at,
    )
    corrected_condition = candidate(
        403,
        news.source_id,
        url="https://www.beograd.rs/lat/beoinfo-vesti/a116403/corrected-condition.html",
        title=news.title,
        body=news_body.replace("privremeno ukida", "privremeno izmešta"),
        published_at=news.published_at,
    )
    recurring_event = candidate(
        404,
        news.source_id,
        url="https://www.beograd.rs/lat/beoinfo-vesti/a116404/recurring-event.html",
        title=news.title,
        body=news_body.replace("23. oktobra", "23. novembra"),
        published_at=news.published_at,
    )

    for distinct in (
        changed_date,
        changed_route,
        changed_stop,
        corrected_condition,
        recurring_event,
    ):
        assert not same_event(service, distinct)


def test_cluster_events_handles_missing_dates_and_keeps_distinct_events() -> None:
    first = candidate(1, "a", url="https://a.test/1", title="Событие один", body="A")
    duplicate = candidate(
        2,
        "b",
        url="https://a.test/1?utm_source=x",
        title="Копия",
        body="B",
        published_at=NOW + timedelta(hours=1),
    )
    undated = candidate(
        3,
        "c",
        url="https://c.test/3",
        title="Другое событие",
        body="C",
        published_at=None,
    )

    clusters = cluster_events([undated, duplicate, first])
    assert [[item.raw_item_id for item in group] for group in clusters] == [[1, 2], [3]]
