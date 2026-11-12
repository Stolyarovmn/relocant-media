"""Импортировать адресные точки кадастра RGZ из публичных отчётов OSM Serbia."""

import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from relocant_media.ops.belgrade_address_index import _write_index
from relocant_media.services.local_outage_geometry import (
    _point,
    _valid_rgz_url,
    address_key,
)

_DATE = re.compile(r"RGZ podaci od ([0-9]{2}\.[0-9]{2}\.[0-9]{4})\.")
_RGZ_ID = re.compile(r"[1-9][0-9]{0,11}")
_PAIR = re.compile(r"([0-9]{2}\.[0-9]{1,8}),\s*([0-9]{2}\.[0-9]{1,8})")


def parse_report(path: Path, municipality: str, settlement_title: str, url: str) -> dict[str, Any]:
    if not _valid_rgz_url(url):
        raise ValueError("Нужна ссылка на опубликованный отчёт RGZ")
    soup = BeautifulSoup(path.read_bytes().decode("utf-8"), "html.parser")
    heading = f"Izveštaj za {settlement_title}"
    if not any(tag.get_text(" ", strip=True) == heading for tag in soup.find_all("h2")):
        raise ValueError("Имя поселения не совпадает с заголовком отчёта")
    footer = soup.find("footer")
    date = _DATE.search(footer.get_text(" ", strip=True) if footer else "")
    if date is None:
        raise ValueError("Отчёт не содержит дату снимка кадастра")
    rows = []
    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) != 7:
            continue
        rgz_id, label, location = (cell.get_text(" ", strip=True) for cell in cells[:3])
        parts = label.rsplit(maxsplit=1)
        coords = _PAIR.fullmatch(location)
        if len(parts) != 2 or not _RGZ_ID.fullmatch(rgz_id) or coords is None:
            continue
        street, number = parts
        if address_key(street, number, municipality) is None:
            continue
        latitude, longitude = _point(float(coords[1]), float(coords[2]))
        rows.append((street, number, municipality, latitude, longitude, rgz_id))
    if not rows:
        raise ValueError("В отчёте нет пригодных адресных точек")
    return {
        "url": url, "snapshot_at": datetime.strptime(date[1], "%d.%m.%Y").date().isoformat(),
        "addresses": sorted(set(rows)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", nargs=4, required=True,
                        metavar=("HTML", "MUNICIPALITY", "SETTLEMENT_TITLE", "URL"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reports = [parse_report(Path(path), municipality, settlement_title, url)
               for path, municipality, settlement_title, url in sorted(args.report)]
    index = {
        "version": 1,
        "source": "Republički geodetski zavod",
        "license_url": "https://data.gov.rs/sr/datasets/adresni-registar/",
        "reports": reports,
    }
    size = _write_index(index, args.output)
    count = sum(len(report["addresses"]) for report in reports)
    print(f"Точек RGZ: {count}, размер архива: {size} байт")


if __name__ == "__main__":
    main()
