from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from relocant_media.collectors import bgsaobracaj_notices as notices
from relocant_media.collectors.factory import build_collector
from relocant_media.domain.freshness import freshness_decision
from relocant_media.domain.road_work_time import overnight_road_windows
from relocant_media.services.primary_discovery import source_for_url
from relocant_media.sources.registry import load_source_file

SOURCE = load_source_file(Path("cities/belgrade/sources/bgsaobracaj_notices.yaml"))
ZONE = ZoneInfo("Europe/Belgrade")
PUBLISHED = datetime(2026, 11, 20, tzinfo=ZONE)
DEPARTMENT = "/vesti/16/privremeni-saobracaj"
ARTICLE = "https://www.bgsaobracaj.rs/vest/5437/test"
SCHEDULE = (
    "Током радова у тунелу биће заузета десна трака ка Новом Београду "
    "20./21.11. и 21./22.11.2026, а ка Дечанској 22./23.11. и 23./24.11.2026, "
    "у времену од 22.00 до 06.00 сати."
)


def article_html(*, body: str = SCHEDULE, department: str = DEPARTMENT) -> str:
    return f"""<div class="entry-title"><h2>Измена режима у тунелу</h2></div>
    <ul class="entry-meta"><li>20/11/2026</li><li><a href="{department}">Одељење</a></li></ul>
    <div class="entry-content article-editor-content">
    <div class="entry-image"><img src="/fixture.png"></div>
    <p><p>{body.strip()}</p></p><footer>Site footer</footer></div>"""


def card(article_id: int, day: str = "2026-11-20", department: str = DEPARTMENT) -> str:
    return f"""<article class="civic-news-card">
    <time class="civic-news-card__date" datetime="{day}"></time>
    <a class="civic-news-card__department" href="{department}">Одељење</a>
    <h2 class="civic-news-card__title"><a href="/vest/{article_id}/test">Радови</a></h2>
    </article>"""


def test_index_keeps_department_and_article_keeps_directions_publication_day_and_body():
    index = card(5018, "2026-04-21") + card(5437) + card(9000, department="/vesti/10/other")
    assert [item.external_id for item in notices.parse_notice_index(index, SOURCE)] == [
        "5018", "5437",
    ]
    article = notices.parse_notice_article(article_html(), SOURCE, ARTICLE)
    assert article.external_id == "5437"
    assert article.url == ARTICLE
    assert article.published_at == PUBLISHED and article.published_at_precision == "day"
    assert "ка Новом Београду" in article.body and "ка Дечанској" in article.body
    assert "Site footer" not in article.body
    assert article.source_html == article_html()


def test_discovery_accepts_legacy_exact_article_and_rejects_foreign_or_list_urls():
    sources = {SOURCE.id: SOURCE}
    assert source_for_url(ARTICLE.replace("/vest/", "/index.php/vest/"), sources) == SOURCE.id
    assert source_for_url(str(SOURCE.url), sources) is None
    assert source_for_url(ARTICLE.replace("www.bgsaobracaj.rs", "other.example"), sources) is None
    with pytest.raises(ValueError, match="different department"):
        notices.parse_notice_article(article_html(department="/vesti/10/other"), SOURCE, ARTICLE)


@pytest.mark.asyncio
async def test_source_uses_dated_cards_skips_pinned_archive_and_keeps_one_working_article(
    monkeypatch,
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 23, 8, tzinfo=ZONE).astimezone(tz)

    monkeypatch.setattr(notices, "datetime", Clock)
    seen = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/vesti":
            return httpx.Response(200, text=(
                card(5018, "2026-04-21") + card(5437) + card(5438) + card(5423, "2026-11-13")
            ))
        if request.url.path == "/vest/5438/test":
            return httpx.Response(404)
        if request.url.path == "/vest/5423/test":
            return httpx.Response(200, text=article_html().replace("20/11/2026", "13/11/2026"))
        return httpx.Response(200, text=article_html())

    async with httpx.AsyncClient(transport=httpx.MockTransport(route), follow_redirects=True) as c:
        collector = build_collector(SOURCE, c)
        items = await collector.collect()
    assert [item.external_id for item in items] == ["5437", "5423"]
    assert "/vest/5018/test" not in seen
    assert [error["error_type"] for error in collector.item_failures] == ["HTTPStatusError"]


@pytest.mark.asyncio
async def test_collect_url_does_not_follow_a_redirect_out_of_the_registered_site():
    seen = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://other.example/"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(route), follow_redirects=True) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await notices.TrafficSecretariatCollector(SOURCE, c).collect_url(ARTICLE)
    assert seen == ["www.bgsaobracaj.rs"]


@pytest.mark.parametrize(("moment", "reason"), [
    ("2026-11-23T07:00", "dated_event_still_relevant"),
    ("2026-11-23T22:20", "dated_event_still_relevant"),
    ("2026-11-24T05:59", "dated_event_still_relevant"),
    ("2026-11-24T06:00", "event_expired"),
    ("2026-11-24T17:00", "event_expired"),
])
def test_freshness_uses_actual_end_of_last_night(moment, reason):
    item = notices.parse_notice_article(article_html(), SOURCE, ARTICLE)
    now = datetime.fromisoformat(moment).replace(tzinfo=ZONE)
    result = freshness_decision(
        item.title, item.body, item.published_at, now,
        published_at_precision=item.published_at_precision,
    )
    assert result.reason == reason
    assert result.action == ("discard" if reason == "event_expired" else "keep")


def test_overnight_schedule_keeps_separate_times_and_crosses_new_year_with_explicit_dates():
    windows = overnight_road_windows(
        "Radovi u tunelu 31.12./1.1.2027. od 22.30 do 05.15",
        datetime(2026, 12, 29, tzinfo=ZONE),
    )
    assert windows == ((datetime(2026, 12, 31, 22, 30, tzinfo=ZONE),
                        datetime(2027, 1, 1, 5, 15, tzinfo=ZONE)),)
    assert not overnight_road_windows(
        "Radovi u tunelu 20./23.11.2026. od 22:00 do 06:00", PUBLISHED,
    )
    assert not overnight_road_windows(
        "Radovi u tunelu 23./24.11.2026. od 08:00 do 16:00", PUBLISHED,
    )
    assert not overnight_road_windows(
        "Radovi u tunelu 23./24.11.2026. od 22:00 do 06:00 i od 23:00 do 04:00", PUBLISHED,
    )
    assert not overnight_road_windows("Koncert 23./24.11.2026. od 22:00 do 06:00", PUBLISHED)
    young = PUBLISHED + timedelta(hours=25)
    assert freshness_decision("Концерт", "Koncert 23./24.11. od 22:00 do 06:00",
                              PUBLISHED, young).reason == "within_news_window"
