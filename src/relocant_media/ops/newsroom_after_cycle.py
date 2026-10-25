"""End-of-cycle public projection and optional GitHub upload, isolated from publication."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy.engine import make_url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    status: dict[str, object] = {"export": "failed", "upload": "not_attempted"}
    try:
        url = make_url(os.environ["DATABASE_URL"])
        if url.get_backend_name() != "sqlite" or not url.database or url.query:
            raise ValueError("existing canonical SQLite required")
        target = args.output / "newsroom-public.json"
        subprocess.run([sys.executable, "-m", "relocant_media.ops.newsroom_export",
                        "--database", url.database, "--output", str(target),
                        "--run-directory", str(args.output)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        status["export"] = "completed"
        if os.environ.get("NEWSROOM_GITHUB_TOKEN"):
            subprocess.run([sys.executable, "-m", "relocant_media.ops.newsroom_publish",
                            "--snapshot", str(target)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            status["upload"] = "completed"
        else:
            status["upload"] = "credential_not_configured"
    except Exception as error:
        status["error_type"] = type(error).__name__
    (args.output / "newsroom-refresh.json").write_text(
        json.dumps(status, indent=2), encoding="utf-8")
    print(json.dumps(status))
    if status["export"] != "completed" or status["upload"] != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
