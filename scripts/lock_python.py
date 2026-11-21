"""Resolve tests and production against one set of Python package versions."""

import argparse
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK_DIR = Path("requirements")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upgrade", action="store_true", help="Resolve newer compatible versions")
    parser.add_argument("--constraints", type=Path, help="Initial versions from a verified install")
    args = parser.parse_args()
    options = ["--upgrade"] if args.upgrade else []
    if args.constraints is not None:
        options.extend(("--constraints", str(args.constraints.resolve(strict=True))))

    def compile_lock(name: str, inputs: list[str]) -> None:
        subprocess.run(
            ["uv", "pip", "compile", "--python-version", "3.12", "--universal",
             "--no-python-downloads", "--default-index", "https://pypi.org/simple",
             "--generate-hashes", "--no-annotate",
             "--custom-compile-command", "python scripts/lock_python.py",
             *options, *inputs, "--output-file", str(LOCK_DIR / f"{name}.lock")],
            cwd=ROOT,
            check=True,
        )

    compile_lock("build", ["requirements/build.in"])
    compile_lock("runtime", ["pyproject.toml", "--constraints", "requirements/build.lock"])
    compile_lock("dev", ["pyproject.toml", "--extra", "dev", "--constraints",
                         "requirements/build.lock", "--constraints", "requirements/runtime.lock"])


if __name__ == "__main__":
    main()
