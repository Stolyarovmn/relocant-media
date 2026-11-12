from datetime import UTC, date, datetime

import pytest

from relocant_media.ai.models import EvidenceDocument
from relocant_media.services.utility_scope import extract_power_addresses, power_outage_date

URL = ("https://www.beograd.rs/lat/servisne-informacije/elektricna-energija/"
       "a116619/Planirana-iskljucenja-na-elektro-mrezi.html")


def document(body="Vračar 9.00 - 11.00 PRVA ULICA: 42,", **changes):
    return EvidenceDocument.model_validate({
        "source_id": "city_beograd_news", "url": URL,
        "title": "ELEKTRIČNA ENERGIJA: Planirana isključenja za 13. novembar 2026. godine",
        "body": body, "published_at": datetime(2026, 11, 12, tzinfo=UTC),
        "published_at_precision": "day", **changes,
    })


def address(municipality, street, number):
    return {"municipality": municipality, "street": street, "number": number}


def test_exact_houses_suffixes_and_different_municipalities():
    value = document("\n\n".join([
        "Zbog izvođenja planiranih radova bez električne energije:",
        "Vračar 9.00 - 11.00 PRVA ULICA: 42,",
        "Zvezdara 09:00 - 11:00 DRUGA ULICA: 2, PRVA ULICA: 57, TREĆA: 1-5, 9A,",
        "Palilula 8.30 - 14.00 ČETVRTA: 119-135A, PETA: 2-4A, 1-5, 11b, 61Đ,",
    ]))
    assert extract_power_addresses(value) == [
        address("Vračar", "PRVA ULICA", "42"),
        address("Zvezdara", "DRUGA ULICA", "2"),
        address("Zvezdara", "PRVA ULICA", "57"),
        address("Zvezdara", "TREĆA", "9A"),
        address("Palilula", "PETA", "11b"),
        address("Palilula", "PETA", "61Đ"),
    ]


def test_settlement_context_is_explicit_and_resets_at_new_row():
    value = document("\n".join([
        "Čukarica 8.30 - 14.00 Naselje ŽARKOVO: PRVA: 2-8, DRUGA: 20-28A, 17, 21,",
        "Novi Beograd 10.00 - 14.00 TREĆA: BB, 5B-19, 35B-35B, 4, "
        "Naselje LEDINE: ČETVRTA: 13A-17B, 15, Naselje NOVI BEOGRAD: PETA: 61Đ,",
        "Čukarica 15.00 - 16.00 ŠESTA: 1,",
    ]))
    assert extract_power_addresses(value) == [
        address("Čukarica, naselje ŽARKOVO", "DRUGA", "17"),
        address("Čukarica, naselje ŽARKOVO", "DRUGA", "21"),
        address("Novi Beograd", "TREĆA", "4"),
        address("Novi Beograd, naselje LEDINE", "ČETVRTA", "15"),
        address("Novi Beograd, naselje NOVI BEOGRAD", "PETA", "61Đ"),
        address("Čukarica", "ŠESTA", "1"),
    ]


def test_cyrillic_names_and_numbered_streets_are_preserved():
    value = document("Земун 08:30 – 12:30 Насеље БАТАЈНИЦА: 13. МАЈА: 3А, 5ђ, 7,",
        title="ЕЛЕКТРИЧНА ЕНЕРГИЈА: Планирана искључења за 13. новембра 2026. године")
    assert extract_power_addresses(value) == [
        address("Земун, naselje БАТАЈНИЦА", "13. МАЈА", "3А"),
        address("Земун, naselje БАТАЈНИЦА", "13. МАЈА", "5ђ"),
        address("Земун, naselje БАТАЈНИЦА", "13. МАЈА", "7"),
    ]


def test_independent_intervals_never_merge_and_continuations_have_no_inherited_scope():
    value = document("\n".join([
        "Vračar 09.00 - 11.00 PRVA: 42,",
        "Zvezdara 11.00 - 13.00 PRVA: 42,",
        "Vračar 14.00 - 16.00 PRVA: 42,",
        "DRUGA: 7,", "13.00 - 15.00 TREĆA: 9,",
        "Novi Sad 08.00 - 10.00 ČETVRTA: 5,",
    ]))
    assert extract_power_addresses(value) == [
        address("Vračar", "PRVA", "42"), address("Zvezdara", "PRVA", "42"),
        address("Vračar", "PRVA", "42"),
    ]


@pytest.mark.parametrize("row", [
    "Vračar 9.00 - 11.00 PRVA: 1-5, 25-25, 5B-19, BB, бб,",
    "Lazarevac 9.00 - 13.00 Naselje Primer (zaseok Drugi)",
    "Vračar 9.00 - 11.00 Naselje PRVO, ULICA: 5,",
    "Vračar 9.00 - 11.00 ULICA: 5, Zvezdara 11.00 - 13.00 DRUGA: 7,",
    "Vračar 9.99 - 11.00 ULICA: 5,", "Vračar 24.00 - 25.00 ULICA: 5,",
    "Vračar 11.00 - 09.00 ULICA: 5,", "Vračar 09.00 - 09.00 ULICA: 5,",
    "Vračar 9.00 - 11.00 ULICA: 0, 15/2, 05, -1, 7.5,",
    "Vračar 9.00 - 11.00 Naselje <script>: PRVA: 5,",
    "Vračar 9.00 - 11.00 BROJ: DRUGI: 7,",
])
def test_ranges_unknown_scope_and_malformed_rows_have_no_points(row):
    assert extract_power_addresses(document(row)) == []


def test_unknown_fragment_does_not_reuse_previous_street_for_later_number():
    assert extract_power_addresses(document(
        "Vračar 09.00 - 11.00 PRVA: 5, nepoznata adresa, 7, DRUGA: 9,")) == [
            address("Vračar", "PRVA", "5"), address("Vračar", "DRUGA", "9")]


@pytest.mark.parametrize("value,expected", [
    ("13. novembar 2026. godine", date(2026, 11, 13)),
    ("13. novembra 2026.", date(2026, 11, 13)),
    ("1. januar 2027. godine", date(2027, 1, 1)),
    ("13.11.2026.", date(2026, 11, 13)),
    ("2026-11-13", date(2026, 11, 13)),
    ("29. februar 2028.", date(2028, 2, 29)),
    ("29. februar 2026.", None), ("31. novembar 2026.", None),
    ("13. novembar", None), ("sutra", None), ("2026-13-13", None),
])
def test_schedule_date_is_explicit_valid_and_never_invented(value, expected):
    item = document(title="ELEKTRIČNA ENERGIJA: Planirana isključenja za " + value)
    before = item.published_at
    assert power_outage_date(item) == expected
    assert item.published_at == before
    assert bool(extract_power_addresses(item)) == (expected is not None)


@pytest.mark.parametrize("changes", [
    {"source_id": "media_reprint"}, {"published_at": None},
    {"published_at": datetime(2026, 11, 12)},
    {"url": URL.replace("https:", "http:")},
    {"url": URL.replace("www.beograd.rs", "www.beograd.rs.evil.example")},
    {"url": URL.replace("www.beograd.rs", "user@www.beograd.rs")},
    {"url": URL.replace("www.beograd.rs", "www.beograd.rs:443")},
    {"url": URL.replace("elektricna-energija", "saobracaj-i-prevoz")},
    {"url": URL + "?date=2026-11-13"}, {"url": URL + "#map"},
    {"url": "https://[bad/path"}, {"url": URL.replace("/lat/", "/\nlat/")},
    {"title": "ELEKTRIČNA ENERGIJA: Nema planiranih isključenja za 13. novembar 2026. godine"},
])
def test_only_dated_exact_official_power_article_is_eligible(changes):
    assert power_outage_date(document(**changes)) is None
    assert extract_power_addresses(document(**changes)) == []


def test_bounded_input_and_output_fail_closed():
    assert extract_power_addresses(document("x" * 100_001)) == []
    assert extract_power_addresses(document("\n".join(
        f"Vračar 09.00 - 11.00 PRVA: {number}," for number in range(1, 302)))) == []
