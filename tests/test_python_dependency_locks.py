import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

LOCK_DIR = Path(__file__).resolve().parents[1] / "requirements"


def locked_versions(profile: str, platform: str, system: str) -> dict[str, str]:
    text = (LOCK_DIR / f"{profile}.lock").read_text(encoding="utf-8")
    versions = {}
    for text_entry in re.split(r"(?m)^(?=[a-zA-Z])", text)[1:]:
        definition, hashes = text_entry.split(" \\\n", 1)
        requirement = Requirement(definition)
        assert re.search(r"--hash=sha256:[a-f0-9]{64}\b", hashes), requirement.name
        name = canonicalize_name(requirement.name)
        if requirement.marker is None or requirement.marker.evaluate({
            "sys_platform": platform,
            "platform_system": system,
            "os_name": "nt" if platform == "win32" else "posix",
            "python_version": "3.12",
            "python_full_version": "3.12.0",
        }):
            assert name not in versions and str(requirement.specifier).startswith("==")
            versions[name] = str(requirement.specifier)
    assert versions
    return versions


@pytest.mark.parametrize(("platform", "system"), [("linux", "Linux"), ("win32", "Windows")])
def test_runtime_and_ci_install_identical_versions(platform: str, system: str):
    runtime = locked_versions("runtime", platform, system)
    dev = locked_versions("dev", platform, system)
    build = locked_versions("build", platform, system)
    assert runtime.items() <= dev.items()
    assert all(build[name] == dev[name] for name in build.keys() & dev.keys())
