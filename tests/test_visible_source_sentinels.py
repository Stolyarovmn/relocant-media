from pathlib import Path

from relocant_media.collectors.natural_history_news import _news_cards
from relocant_media.collectors.yugoslavia_programs import _program_links
from relocant_media.sources.registry import load_source_file


def test_natural_history_visible_heading_does_not_require_heading_tag() -> None:
    source = load_source_file(Path("cities/belgrade/sources/venue_natural_history.yaml"))
    html = """
    <html><body>
      <div>Вести</div>
      <h3><a href="/primer-vesti/">Пример вести</a></h3>
      <p>Кратак опис вести.</p>
      <a href="/primer-vesti/">Прочитај више »</a>
      <span>22.10.2026.</span>
    </body></html>
    """

    cards = _news_cards(html, source)

    assert len(cards) == 1
    assert cards[0][0] == "https://nhmbeo.rs/primer-vesti/"
    assert cards[0][1] == "Пример вести"


def test_yugoslavia_visible_heading_does_not_require_heading_tag() -> None:
    source = load_source_file(Path("cities/belgrade/sources/venue_yugoslavia.yaml"))
    html = """
    <html><body>
      <div>ИЗЛОЖБЕ И ПРОГРАМИ</div>
      <a href="/rs/program/primer-programa/">Програм Пример програма</a>
      <a href="/rs/izlozba/primer-izlozbe/">Изложба Пример изложбе</a>
    </body></html>
    """

    links = _program_links(html, source)

    assert links == ["https://muzej-jugoslavije.org/rs/program/primer-programa/"]
