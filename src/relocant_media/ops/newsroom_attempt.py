"""Record a hosted attempt without exposing an uncheckpointed working database."""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from relocant_media.ops.newsroom_export import snapshot
from relocant_media.ops.newsroom_merge import merge


def fetch_previous(path: Path, token: str) -> None:
    """Only 404 means absent; authorization/network errors must preserve the site."""
    if not token:
        raise ValueError("public snapshot read credential required")
    path.unlink(missing_ok=True)
    with httpx.Client(timeout=25, follow_redirects=False) as client:
        response = client.get(
            "https://api.github.com/repos/Stolyarovmn/relocant-media/contents/"
            "dashboard-data/latest.json", params={"ref": "newsroom-data"},
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"})
    if response.status_code == 404:
        return
    response.raise_for_status()
    record = response.json()
    if record.get("encoding") != "base64":
        raise ValueError("unsupported public snapshot encoding")
    content = base64.b64decode("".join(record["content"].split()), validate=True)
    if len(content) > 900_000:
        raise ValueError("public snapshot too large")
    path.write_bytes(content)


def record_attempt(root: Path, output: Path, previous: Path, *, outcome: str,
                   mode: str, run_id: str, run_attempt: str) -> dict[str, Any]:
    if outcome not in {"success", "failure", "cancelled", "skipped"}:
        raise ValueError("unsupported Actions outcome")
    if mode not in {"preview", "editorial", "intake", "discovery", "not_selected"}:
        raise ValueError("unsupported worker mode")
    if not all(re.fullmatch(r"[0-9]{1,20}", value) for value in (run_id, run_attempt)):
        raise ValueError("numeric Actions identifiers required")
    # actions_runtime removes output on exceptions. Only its surviving output has
    # passed the checkpoint (or is a verified, read-only preview).
    verified = output.is_file()
    source = output if verified else previous
    payload = snapshot(root)
    if source.is_file():
        payload = merge(payload, json.loads(source.read_text(encoding="utf-8")))
    payload["last_attempt"] = {
        "status": outcome, "mode": mode, "run_id": run_id, "run_attempt": run_attempt,
        "observed_at": datetime.now(UTC).isoformat(),
        "projection": "verified" if verified else "previous_or_unavailable",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(output)
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--outcome", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--fetch-previous", action="store_true")
    args = parser.parse_args()
    try:
        if args.fetch_previous and not args.output.is_file():
            fetch_previous(args.previous, os.environ.get("NEWSROOM_GITHUB_TOKEN", ""))
        record_attempt(args.root, args.output, args.previous, outcome=args.outcome,
                       mode=args.mode, run_id=args.run_id, run_attempt=args.run_attempt)
    except Exception as error:
        # Do not print HTTP URLs, bodies, tokens or exception text into public logs.
        parser.exit(1, f"newsroom_attempt: {type(error).__name__}\n")


if __name__ == "__main__":
    main()
