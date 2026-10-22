"""Resolve current source IDs from changed registry paths for strict CI probes."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from relocant_media.sources.registry import load_source_file

DEFAULT_SOURCE_DIR = Path("cities/belgrade/sources")


def changed_source_ids(
    paths: Iterable[str],
    *,
    repository_root: Path = Path("."),
    source_dir: Path = DEFAULT_SOURCE_DIR,
) -> tuple[str, ...]:
    """Return unique current source IDs for changed YAML files; deleted files are skipped."""
    if source_dir.is_absolute():
        raise ValueError("source directory must be repository-relative")

    source_ids: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        value = raw.strip()
        if not value:
            continue
        relative = Path(value)
        if relative.is_absolute() or relative.parent != source_dir or relative.suffix != ".yaml":
            continue
        current = repository_root / relative
        if not current.exists():
            continue
        if current.is_symlink() or not current.is_file():
            raise ValueError(f"changed source config must be a regular file: {relative}")
        source_id = load_source_file(current).id
        if source_id in seen:
            continue
        seen.add(source_id)
        source_ids.append(source_id)
    return tuple(source_ids)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Print source IDs for changed current registry YAML paths read from stdin"
    )
    parser.add_argument("--repository-root", type=Path, default=Path("."))
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE_DIR)
    args = parser.parse_args()

    for source_id in changed_source_ids(
        sys.stdin,
        repository_root=args.repository_root,
        source_dir=args.source_dir,
    ):
        print(source_id)


if __name__ == "__main__":
    main()
