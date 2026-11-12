from pathlib import Path

import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.services.local_street_geometry import StreetIndex, load_street_index
from relocant_media.services.road_scope import is_road_restriction, resolve_road_segments
from relocant_media.services.utility_geography import _name


def notice(body: str) -> EvidenceDocument:
    return EvidenceDocument(
        source_id="bgprevoz_changes",
        url="https://www.bgprevoz.rs/linije/aktuelne-izmene/1134",
        title="Информација о измени саобраћаја",
        body="Период измене: 14.10.2026–15.11.2026.\n" + body,
    )


@pytest.fixture(scope="module")
def index() -> StreetIndex:
    return load_street_index(Path("cities/belgrade/map/roads.json.gz"))


@pytest.mark.parametrize(("body", "street", "length"), [
    (
        "Радови на уређењу улице Гетеова (ГО Земун) мењају саобраћај. "
        "Радови се изводе на коловозу и на тротоару, на делу од раскрснице са "
        "улицом Пазовачки пут до раскрснице са улицом Крајишка.",
        "Гетеова",
        19,
    ),
    (
        "Radovi se izvode na kolovozu u ulici Teodora Hercla, na delu od raskrsnice "
        "sa ulicom Novogradska do raskrsnice sa ulicom Tošin bunar (raskrsnica ostaje otvorena).",
        "Теодора Херцла",
        31,
    ),
    (
        "Током радова на коловозу у улици Војвођанска, на делу од улице Хајдук Вељкова "
        "до улице Десанке Максимовић, доћи ће до заузећа средишњег дела коловоза.",
        "Војвођанска",
        8,
    ),
])
def test_road_scope_resolves_explicit_intersections(index, body, street, length):
    document = notice(body)
    assert is_road_restriction(document)
    line, = resolve_road_segments(document, index)
    assert _name(line["segment"]["street"]) == _name(street)
    assert len(line["coordinates"]) == length
    assert len(set(tuple(point) for point in line["coordinates"])) == length
    assert line["osm_urls"]


@pytest.mark.parametrize("body", [
    "Измешта се стајалиште у улици Гетеова, на делу од улице Пазовачки пут до улице Крајишка.",
    "Мења се траса линије 15 на делу од улице Пазовачки пут до улице Крајишка.",
    "Изводе се радови на реконструкцији стајалишта у улици Гетеова.",
])
def test_stop_or_route_alone_is_not_a_road_restriction(index, body):
    document = notice(body)
    assert not is_road_restriction(document)
    assert not resolve_road_segments(document, index)


@pytest.mark.parametrize("body", [
    "Улица Дољанска затвара се за саобраћај у висини моста у насељу Грабовац.",
    "Радови у улици Војвођанска на делу од улице Нишка до улице Шекспирова.",
    "У улици Гетеова се изводе радови. Возила ће саобраћати улицом Пазовачки пут.",
    "Радови на коловозу у улици Гетеова. Возила ће саобраћати улицом "
    "Гетеова на делу од улице Пазовачки пут до улице Крајишка.",
    "Фаза 2: Радови у улици Гетеова на делу од улице Пазовачки пут до улице Крајишка. "
    "Фаза 3: Радови у улици Војвођанска на делу од улице Хајдук Вељкова до "
    "улице Десанке Максимовић.",
])
def test_work_without_unique_current_extent_does_not_produce_line(index, body):
    document = notice(body)
    assert is_road_restriction(document)
    assert not resolve_road_segments(document, index)


def test_nonofficial_document_cannot_claim_road_geometry(index):
    document = notice(
        "Радови у улици Гетеова на делу од улице Пазовачки пут до улице Крајишка.",
    ).model_copy(update={"url": "https://example.com/linije/aktuelne-izmene/1134"})
    assert not is_road_restriction(document)
    assert not resolve_road_segments(document, index)


def test_repeated_segment_and_reverse_description_are_deduplicated(index):
    document = notice(
        "Радови у улици Гетеова на делу од улице Пазовачки пут до улице Крајишка. "
        "Радови у улици Гетеова на делу од улице Крајишка до улице Пазовачки пут.",
    )
    assert len(resolve_road_segments(document, index)) == 1
