"""Собрать общие статические файлы обоих путей публикации GitHub Pages."""

import argparse
import gzip
import shutil
from pathlib import Path


def copy_assets(root: Path, site: Path) -> None:
    shutil.copytree(root / "web/newsroom", site, dirs_exist_ok=True)
    compressed = root / "cities/belgrade/map/addresses.json.gz"
    shutil.copyfile(compressed, site / "map/addresses.json.gz")
    destination = site / "map/addresses.json"
    temporary = destination.with_suffix(".tmp")
    with gzip.open(compressed, "rb") as source:
        with temporary.open("wb") as target:
            shutil.copyfileobj(source, target)
    temporary.replace(destination)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site", type=Path, required=True)
    args = parser.parse_args()
    copy_assets(Path.cwd(), args.site)


if __name__ == "__main__":
    main()
