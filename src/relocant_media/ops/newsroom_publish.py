"""Publish only the allowlisted newsroom projection to a dedicated GitHub data branch."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path

import httpx

from relocant_media.ops.map_export import MapFeed, read_map_feed, write_map_feed
from relocant_media.ops.newsroom_export import snapshot
from relocant_media.ops.newsroom_merge import merge

REPOSITORY = "Stolyarovmn/relocant-media"
BRANCH = "newsroom-data"


MAP_PATH = "contents/dashboard-data/map.json"


def _publish_map(client: httpx.Client, path: Path) -> None:
    current = client.get(MAP_PATH, params={"ref": BRANCH},
                         headers={"Accept": "application/vnd.github.object+json"})
    if current.status_code != 404:
        current.raise_for_status()
    if not path.is_file():
        if current.status_code == 404:
            write_map_feed(path, MapFeed())
            return
        response = client.get(MAP_PATH, params={"ref": BRANCH},
                              headers={"Accept": "application/vnd.github.raw+json"})
        response.raise_for_status()
        write_map_feed(path, read_map_feed(response.content))
        return
    feed = read_map_feed(path.read_bytes())
    content = feed.model_dump_json(exclude_none=True).encode("utf-8")
    payload = {"message": "Update public Belgrade map", "branch": BRANCH,
               "content": base64.b64encode(content).decode("ascii")}
    if current.status_code == 200:
        payload["sha"] = current.json()["sha"]
    client.put(MAP_PATH, json=payload).raise_for_status()


def publish(path: Path, token: str, *, map_path: Path | None = None) -> None:
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
        if map_path is not None:
            _publish_map(client, map_path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--map-snapshot", type=Path)
    args = parser.parse_args()
    token = os.environ.get("NEWSROOM_GITHUB_TOKEN", "")
    if not token:
        parser.exit(2, "newsroom_publish: credential_not_configured\n")
    try:
        publish(args.snapshot, token, map_path=args.map_snapshot)
    except Exception as error:
        parser.exit(1, f"newsroom_publish: {type(error).__name__}\n")
    print("newsroom_publish: uploaded")


if __name__ == "__main__":
    main()
