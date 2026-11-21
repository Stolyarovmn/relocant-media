"""Install the application against a hash-checked lock without re-resolving."""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pip(*args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "--disable-pip-version-check", *args], cwd=ROOT, check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "profile", choices=("runtime", "dev", "image"), nargs="?", default="runtime",
    )
    profile = parser.parse_args().profile
    lock = "dev" if profile == "dev" else "runtime"
    pip("install", "--only-binary=:all:", "--require-hashes", "-r", "requirements/build.lock")
    pip("install", "--no-build-isolation", "--require-hashes", "-r", f"requirements/{lock}.lock")
    if profile == "image":
        pip("install", "--no-build-isolation", "--no-deps", ".")
    else:
        pip("install", "--no-build-isolation", "--no-deps", "-e", ".")
    pip("check")


if __name__ == "__main__":
    main()
