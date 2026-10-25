"""Publish only the allowlisted newsroom projection to a dedicated GitHub data branch."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

import httpx

from relocant_media.ops.newsroom_export import snapshot
from relocant_media.ops.newsroom_merge import merge

REPOSITORY = "Stolyarovmn/relocant-media"
BRANCH = "newsroom-data"


def publish(path: Path, token: str) -> None:
    # Reconstruct using exporter output only: this CLI must not upload arbitrary run artifacts.
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or data.get("mode") != "public_read_only":
        raise ValueError("newsroom projection required")
    data = merge(snapshot(Path.cwd()), data)
    content = json.dumps(data, ensure_ascii=False).encode("utf-8")
    if len(content) > 900_000:
        raise ValueError("snapshot too large")
    with httpx.Client(base_url=f"https://api.github.com/repos/{REPOSITORY}/", timeout=25,
                      headers={"Authorization": f"Bearer {token}",
                               "Accept": "application/vnd.github+json"},
                      follow_redirects=False) as client:
        ref = client.get(f"git/ref/heads/{BRANCH}")
        if ref.status_code == 404:
            main = client.get("git/ref/heads/main")
            main.raise_for_status()
            created = client.post("git/refs", json={"ref": f"refs/heads/{BRANCH}",
                                                   "sha": main.json()["object"]["sha"]})
            created.raise_for_status()
        else:
            ref.raise_for_status()
        current = client.get("contents/dashboard-data/latest.json", params={"ref": BRANCH})
        payload = {"message": "Update public newsroom snapshot", "branch": BRANCH,
                   "content": base64.b64encode(content).decode("ascii")}
        if current.status_code == 200:
            payload["sha"] = current.json()["sha"]
        elif current.status_code != 404:
            current.raise_for_status()
        response = client.put("contents/dashboard-data/latest.json", json=payload)
        response.raise_for_status()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    token = os.environ.get("NEWSROOM_GITHUB_TOKEN", "")
    if not token:
        parser.exit(2, "newsroom_publish: credential_not_configured\n")
    try:
        publish(args.snapshot, token)
    except Exception as error:
        parser.exit(1, f"newsroom_publish: {type(error).__name__}\n")
    print("newsroom_publish: uploaded")


if __name__ == "__main__":
    main()
