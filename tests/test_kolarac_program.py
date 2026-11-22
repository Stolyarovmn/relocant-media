from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    NameObject,
    NumberObject,
)

from relocant_media.ai.models import EvidenceDocument
from relocant_media.collectors.kolarac_program import (
    MAX_PROGRAM_BYTES,
    linked_program,
    parse_linked_program,
)
from relocant_media.collectors.venue_feed import VenueFeedCollector
from relocant_media.domain.city_profile import load_city_profile
from relocant_media.domain.freshness import freshness_decision
from relocant_media.services.event_place import resolve_event_place
from relocant_media.sources.registry import load_source_registry

ARTICLE = "https://www.kolarac.rs/predavanja/23-11-2026-u-18/"
PATH = "/wp-content/uploads/2026/11/program.pdf"
PROGRAM = "https://www.kolarac.rs" + PATH
PAGE = (
    '<h1>23. 11. 2026. у 18</h1><div class="entry-content"><img src="/poster.webp">'
    f'<a href="http://www.kolarac.rs{PATH}">ДЕТАЉНИЈЕ</a></div>'
)
START = datetime(2026, 11, 23, 18)
TITLE = "O ARHITEKTURI I GRADU"
HALL = "Velikoj dvorani Kolarčeve zadužbine"


def _pdf(*lines: str) -> bytes:
    writer = PdfWriter()
    page = writer.add_blank_page(width=595, height=842)
    name = NameObject
    font = DictionaryObject({
        name("/Type"): name("/Font"),
        name("/Subtype"): name("/Type1"),
        name("/BaseFont"): name("/Helvetica"),
        name("/Encoding"): DictionaryObject({
            name("/BaseEncoding"): name("/WinAnsiEncoding"),
            name("/Differences"): ArrayObject([NumberObject(232), name("/ccaron")]),
        }),
    })
    page[name("/Resources")] = DictionaryObject({
        name("/Font"): DictionaryObject({name("/F1"): font}),
    })
    content = DecodedStreamObject()
    operators = [f"1 0 0 1 24 {760 - index * 18} Tm ({line}) Tj"
                 for index, line in enumerate(lines)]
    content.set_data(("BT /F1 12 Tf\n" + "\n".join(operators) + "\nET").encode("cp1250"))
    page.replace_contents(content)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.mark.parametrize("date_text", ["23. novembra u 18", "23. novembar 2026. u 18.00"])
def test_verified_schedule_from_pdf_keeps_provenance(date_text):
    body = _pdf(TITLE, "Javno predavanje o urbanizmu.", f"{date_text}, u {HALL}.")
    title, text = parse_linked_program(body, PROGRAM, START)
    assert title == TITLE
    assert "23.11.2026. у 18:00" in text.splitlines()[0]
    assert HALL in text.splitlines()[0]
    assert text.endswith(PROGRAM)
    verdict = freshness_decision(title, text, datetime(2026, 11, 18, tzinfo=UTC),
                                 datetime(2026, 11, 22, tzinfo=UTC))
    assert verdict.reason == "dated_event_still_relevant"


@pytest.mark.parametrize("contents", [
    ("24. novembra u 18", HALL),
    ("23. novembra u 19", HALL),
    ("23. novembra 2025. u 18", HALL),
    ("23. novembra u 18", "Nepoznata sala"),
    ("23. novembra u 18", HALL, "24. novembra u 19"),
])
def test_pdf_requires_unambiguous_matching_start_and_hall(contents):
    with pytest.raises(ValueError, match="Kolarac program"):
        parse_linked_program(_pdf(TITLE, *contents), PROGRAM, START)


@pytest.mark.parametrize("modify", [
    lambda page: page.replace("http://www.kolarac.rs", "https://other.example"),
    lambda page: page.replace("http://www.kolarac.rs", "https://www.kolarac.rs@other.example"),
    lambda page: page.replace(PATH, PATH + "?download=true"),
    lambda page: page.replace(PATH, "/uploads/program.pdf"),
    lambda page: page.replace(PATH, PATH.replace("program", "nested%2fprogram")),
    lambda page: page.replace("23. 11.", "31. 11."),
    lambda page: page.replace("ДЕТАЉНИЈЕ", "Галерија"),
    lambda page: page.replace("</div>", f'<a href="{PATH}.pdf">ДЕТАЉНИЈЕ</a></div>'),
])
def test_unrelated_or_ambiguous_page_does_not_supply_pdf(modify):
    assert linked_program(modify(PAGE), ARTICLE) is None


def test_saved_program_needs_exact_link_text_time_and_hall_for_map():
    title, body = parse_linked_program(_pdf(TITLE, f"23. novembra u 18, u {HALL}."), PROGRAM, START)
    document = EvidenceDocument(source_id="venue_kolarac", url=ARTICLE, title=title, body=body)
    profile = load_city_profile("belgrade")
    place = resolve_event_place(document, PAGE, profile=profile)
    assert place is not None
    assert place["starts_at"] == "2026-11-23T18:00:00+01:00" and place["ends_at"] is None
    assert place["schedule_source_url"] == PROGRAM
    assert place["venue"]["name"] == "Kolarac · Velika dvorana"
    assert place["venue"]["address"] == "Studentski trg 5, Beograd"
    another_program = PAGE.replace(PATH, "/wp-content/uploads/2026/11/other.pdf")
    assert resolve_event_place(document, another_program, profile=profile) is None
    assert resolve_event_place(document, PAGE.replace("у 18", "у 19"), profile=profile) is None
    assert resolve_event_place(document, PAGE.replace("</div>", "Предавање је одложено.</div>"),
                               profile=profile) is None
    for tampered in (body.replace(HALL, "Nepoznata sala"), body.replace("18:00", "19:00"),
                     body.replace("u " + HALL, "Predavanje je otkazano, u " + HALL)):
        changed = document.model_copy(update={"body": tampered})
        assert resolve_event_place(changed, PAGE, profile=profile) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["good", "redirect", "html", "oversize", "unreadable"])
async def test_discovery_and_live_recheck_use_same_source_contract(kind):
    publication = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=1)
    xml = (
        '<rss><channel><item><title>23. 11. 2026. у 18</title>'
        f'<link>{ARTICLE}</link><pubDate>{publication.isoformat()}</pubDate>'
        '</item></channel></rss>'
    )
    source = next(row for row in load_source_registry(Path("cities/belgrade/sources"))
                  if row.id == "venue_kolarac")
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.path == "/feed/":
            return httpx.Response(200, text=xml)
        if str(request.url) == ARTICLE:
            return httpx.Response(200, text=PAGE)
        assert str(request.url) == PROGRAM
        if kind == "redirect":
            return httpx.Response(302, headers={"location": "https://other.example/program.pdf"})
        if kind == "html":
            return httpx.Response(200, text="<html>Not a program</html>")
        if kind == "oversize":
            data = b" " * (MAX_PROGRAM_BYTES + 1)
        elif kind == "unreadable":
            data = b"%PDF-1.5\nbroken"
        else:
            data = _pdf(TITLE, f"23. novembra u 18, u {HALL}.")
        return httpx.Response(200, content=data, headers={"content-type": "application/pdf"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = VenueFeedCollector(source, client)
        if kind == "good":
            item, = await collector.collect()
            assert item == await collector.collect_url(ARTICLE, require_page=True)
            assert item.title == TITLE
            assert item.published_at == publication
            assert item.source_html == PAGE
            assert item.url == ARTICLE and item.body.endswith(PROGRAM)
            assert requested.count(PROGRAM) == 2
        else:
            with pytest.raises(ValueError, match="failed extraction"):
                await collector.collect()
            assert collector.item_failures[0]["url"] == ARTICLE
            assert requested[-1] == PROGRAM
