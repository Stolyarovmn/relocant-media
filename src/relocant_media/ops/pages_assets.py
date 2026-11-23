"""Собрать общие статические файлы обоих путей публикации GitHub Pages."""

import argparse
import gzip
import hashlib
import shutil
from pathlib import Path

_MAP_REVISION = b"?v=__MAP_REVISION__"


def _version_map_shell(source: Path, destination: Path) -> None:
    assets = {
        path: path.read_bytes()
        for path in sorted(source.iterdir())
        if path.is_file() and path.suffix in {".html", ".js", ".css"}
    }
    digest = hashlib.sha256()
    for path, content in assets.items():
        digest.update(path.name.encode() + b"\0" + hashlib.sha256(content).digest())
    version = b"?v=" + digest.hexdigest()[:16].encode()
    for path, content in assets.items():
        if _MAP_REVISION in content:
            (destination / path.name).write_bytes(content.replace(_MAP_REVISION, version))


def copy_assets(root: Path, site: Path) -> None:
    web = root / "web/newsroom"
    shutil.copytree(web, site, dirs_exist_ok=True)
    _version_map_shell(web / "map", site / "map")
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
