from pathlib import Path

from relocant_media.ops.changed_source_ids import changed_source_ids


def _write_source(path: Path, source_id: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"id: {source_id}",
                f"name: {source_id}",
                "publisher: Test publisher",
                "ownership: official",
                "url: https://example.test/source",
                "language: sr-Latn",
                "source_type: official_test",
                "trust: 1.0",
                "check_every: 1h",
                "categories:",
                "  - city",
                "extraction:",
                "  strategy: page_change_detection",
                "  status: active",
                "policy:",
                "  high_risk: false",
                "  auto_publish: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def test_changed_source_ids_reads_current_config_ids(tmp_path: Path) -> None:
    source_dir = Path("cities/belgrade/sources")
    _write_source(tmp_path / source_dir / "renamed-file.yaml", "stable_source_id")

    assert changed_source_ids(
        ["cities/belgrade/sources/renamed-file.yaml"],
        repository_root=tmp_path,
        source_dir=source_dir,
    ) == ("stable_source_id",)


def test_changed_source_ids_skips_deleted_and_non_yaml_paths(tmp_path: Path) -> None:
    source_dir = Path("cities/belgrade/sources")
    _write_source(tmp_path / source_dir / "kept.yaml", "kept")

    assert changed_source_ids(
        [
            "cities/belgrade/sources/deleted.yaml",
            "cities/belgrade/sources/README.md",
            "src/relocant_media/collectors/example.py",
            "cities/belgrade/sources/kept.yaml",
        ],
        repository_root=tmp_path,
        source_dir=source_dir,
    ) == ("kept",)


def test_changed_source_ids_deduplicates_same_source_id(tmp_path: Path) -> None:
    source_dir = Path("cities/belgrade/sources")
    _write_source(tmp_path / source_dir / "one.yaml", "same")
    _write_source(tmp_path / source_dir / "two.yaml", "same")

    assert changed_source_ids(
        [
            "cities/belgrade/sources/one.yaml",
            "cities/belgrade/sources/two.yaml",
        ],
        repository_root=tmp_path,
        source_dir=source_dir,
    ) == ("same",)


def test_changed_source_ids_rejects_absolute_source_directory(tmp_path: Path) -> None:
    try:
        changed_source_ids([], repository_root=tmp_path, source_dir=tmp_path / "sources")
    except ValueError as error:
        assert "repository-relative" in str(error)
    else:
        raise AssertionError("absolute source directory must fail")
