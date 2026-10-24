"""DB-free TypeSafe connectivity check; never collect, select, or publish news."""

import argparse
import json
from pathlib import Path
from typing import Any

from relocant_media.ai.jev import smoke_shadow


def run_smoke(output: Path) -> dict[str, Any]:
    # Do not overwrite a previous measurement. No DB, Gemini, or Telegram imports.
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "kind": "jev_api_smoke",
        "production_benchmark": False,
        "newsroom_state_accessed": False,
        "telegram_transport": "none",
        "fixture_source": "https://docs.typesafe.ai/api/system-one",
        "jev_shadow": smoke_shadow(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = run_smoke(args.output)
    print(json.dumps(manifest, ensure_ascii=False))
    # A failed smoke must be visible; normal editorial Jev remains fail-open.
    if manifest["jev_shadow"]["status"] not in {"ok", "disabled"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
