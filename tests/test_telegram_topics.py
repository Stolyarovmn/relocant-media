from datetime import UTC, datetime, timedelta

import httpx
import pytest

from relocant_media.analytics.public_preview import PreviewError
from relocant_media.collectors.telegram_topics import collect_topics, parse_topics

NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)


def card(number: int, date: str, text: str = "Новость") -> str:
    return f'''<div class="tgme_widget_message" data-post="example/{number}">
    <time datetime="{date}"></time><div class="tgme_widget_message_text">{text}</div>
    <span class="tgme_widget_message_views">1.2K</span></div>'''


def test_only_recent_posts_extract_text_and_public_links() -> None:
    html = card(1, "2026-10-10T18:00:00Z", "Старое") + card(
        2, "2026-10-20T17:00:00Z",
        '<a href="https://beograd.rs/news">Первоисточник</a>'
        '<a href="javascript:alert(1)">Плохое</a>'
        '<a href="https://user:secret@host.test/">Закрытое</a>',
    ) + card(3, "2026-10-21T18:00:00Z", "Из будущего")
    leads = parse_topics(html, "example", NOW)
    assert len(leads) == 1
    lead = leads[0]
    assert lead.message_id == 2
    assert lead.published_at == datetime(2026, 10, 20, 17, tzinfo=UTC)
    assert lead.url == "https://t.me/example/2"
    assert lead.outbound_urls == ("https://beograd.rs/news",)
    assert lead.views and lead.views.value == 1200 and lead.views.approximate
    assert lead.requires_independent_verification is True
    assert "Старое" not in repr(leads)


def test_missing_date_media_only_missing_metrics_and_duplicate() -> None:
    html = card(1, "bad") + card(2, "2026-10-20T17:00:00Z", "")
    html += card(3, "2026-10-20T17:00:00Z").replace(
        '<span class="tgme_widget_message_views">1.2K</span>', ""
    )
    leads = parse_topics(html + html, "example", NOW)
    assert len(leads) == 1
    assert leads[0].views is None


def test_freshness_boundaries_and_input_validation() -> None:
    assert len(parse_topics(card(1, "2026-10-19T18:00:00Z"), "example", NOW)) == 1
    assert parse_topics(card(1, "2026-10-19T17:59:59Z"), "example", NOW) == []
    with pytest.raises(PreviewError):
        parse_topics("", "../evil", NOW)
    with pytest.raises(PreviewError):
        parse_topics("", "example", NOW, max_age=timedelta(0))
    with pytest.raises(PreviewError):
        parse_topics("", "example", NOW.replace(tzinfo=None))


@pytest.mark.asyncio
async def test_public_get_only_no_redirects_or_account() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=card(1, "2026-10-20T17:00:00Z"))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), trust_env=False
    ) as client:
        assert len(await collect_topics("example", client, now=NOW)) == 1
    assert len(requests) == 1
    assert str(requests[0].url) == "https://t.me/s/example"
    assert requests[0].method == "GET"
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("status,html", [(302, ""), (200, "Login"), (500, "")])
async def test_unavailable_preview_fails_closed(status: int, html: str) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(status, text=html)
    ), trust_env=False) as client:
        with pytest.raises(PreviewError):
            await collect_topics("example", client, now=NOW)
