from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from relocant_media.domain.city_profile import CityEditorialProfile, load_city_profile


def second_city_profile() -> CityEditorialProfile:
    values = load_city_profile("belgrade").model_dump(mode="json")
    values.update(
        policy_version="tbilisi-test-v1", city_id="tbilisi", city_name="Тбилиси",
        country_code="GE", country_name="Грузия", timezone="Asia/Tbilisi",
        audience={"name": "Русскоязычные жители Тбилиси", "needs": ["Понимать жизнь города"]},
        geography={"city_aliases": ["Тбилиси", "Tbilisi"],
                   "country_aliases": ["Грузия", "Georgia"], "allow_country_scope": True},
    )
    return CityEditorialProfile.model_validate(values)


def test_profile_loads_real_city_and_roundtrips_second_country(tmp_path: Path) -> None:
    belgrade = load_city_profile("belgrade")
    assert belgrade.rubric("city_changes").cadence == "daily"
    assert belgrade.rubric("weekend").target_per_period == 2
    assert belgrade.rubric("unknown") is None
    second = second_city_profile()
    folder = tmp_path / second.city_id
    folder.mkdir()
    (folder / "editorial.yaml").write_text(
        yaml.safe_dump(second.model_dump(mode="json"), allow_unicode=True), encoding="utf-8"
    )
    assert load_city_profile("tbilisi", cities_root=tmp_path) == second
    assert second.sha256 != belgrade.sha256
    assert len(second.sha256) == 64


@pytest.mark.parametrize("city", ["unknown", "../belgrade", "BELGRADE", "", "belgrade/x"])
def test_no_silent_fallback_for_unknown_or_unsafe_city(city: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_city_profile(city, cities_root=tmp_path)


@pytest.mark.parametrize("mutation", [
    "timezone", "duplicate_rubric", "unknown_field", "empty_need", "wrong_version",
    "weak_threshold", "unsupported_language", "invalid_schedule",
])
def test_invalid_profile_fails_closed(mutation: str) -> None:
    values = load_city_profile("belgrade").model_dump(mode="json")
    if mutation == "timezone":
        values["timezone"] = "Mars/Unknown"
    elif mutation == "duplicate_rubric":
        values["rubrics"].append(values["rubrics"][0])
    elif mutation == "unknown_field":
        values["api_key"] = "must-never-be-a-profile-setting"
    elif mutation == "empty_need":
        values["audience"]["needs"] = ["  "]
    elif mutation == "wrong_version":
        values["schema_version"] = 2
    elif mutation == "weak_threshold":
        values["utility_min_practical_value"] = 2
    elif mutation == "unsupported_language":
        values["content_language"] = "en"
    else:
        values["rubrics"][0]["target_per_period"] = 15
    with pytest.raises(ValidationError):
        CityEditorialProfile.model_validate(values)


def test_profile_hash_binds_editorial_and_schedule_settings() -> None:
    profile = second_city_profile()
    changed = profile.model_dump(mode="json")
    changed["rubrics"][0]["purpose"] = "Другой проверяемый редакционный замысел"
    assert CityEditorialProfile.model_validate(changed).sha256 != profile.sha256
    changed = profile.model_dump(mode="json")
    changed["rubrics"][0]["target_per_period"] = 2
    assert CityEditorialProfile.model_validate(changed).sha256 != profile.sha256


def test_directory_and_declared_city_must_match(tmp_path: Path) -> None:
    folder = tmp_path / "belgrade"
    folder.mkdir()
    (folder / "editorial.yaml").write_text(
        yaml.safe_dump(second_city_profile().model_dump(mode="json")), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="не совпадает"):
        load_city_profile("belgrade", cities_root=tmp_path)
