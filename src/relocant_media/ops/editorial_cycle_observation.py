"""Report a bounded, sanitized decision summary from a private Actions snapshot."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient

_TOKEN = re.compile(r"[a-zA-Z0-9_.:-]{1,80}")


def _token(value: object) -> str | None:
    return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None


def observe_cycle(snapshot: Path, started_at: str) -> dict[str, Any] | None:
    uri = snapshot.resolve(strict=True).as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        cycle = db.execute(
            """SELECT id, status, details FROM scheduler_run
               WHERE task_key = 'editorial-primary:belgrade'
                 AND julianday(started_at) >= julianday(?)
               ORDER BY id DESC LIMIT 1""",
            (started_at,),
        ).fetchone()
        if cycle is None:
            return None
        cycle_id, status, raw_details = cycle
        details = json.loads(raw_details)
        materials = []
        for raw_id in details.get("processed_raw_item_ids", [])[:5]:
            row = db.execute(
                """SELECT raw_item.source_id, editorial_run.outcome,
                          editorial_run.delivery_status, post.telegram_message_id
                   FROM raw_item
                   LEFT JOIN editorial_run ON editorial_run.raw_item_id = raw_item.id
                   LEFT JOIN post ON post.id = editorial_run.post_id
                   WHERE raw_item.id = ?""",
                (raw_id,),
            ).fetchone()
            if row is None:
                continue
            source, raw_outcome, delivery, message_id = row
            outcome = json.loads(raw_outcome) if raw_outcome else {}
            presentation = (outcome.get("_package") or {}).get("presentation") or {}
            materials.append({
                "raw_item_id": raw_id, "source_id": _token(source),
                "status": _token(outcome.get("status")), "reason": _token(outcome.get("reason")),
                "rubric": _token(presentation.get("rubric_id")),
                "has_map": bool(presentation.get("source_map")),
                "illustration": _token((presentation.get("illustration") or {}).get(
                    "illustration_mode")),
                "delivery": _token(delivery), "telegram_message_id": message_id,
            })
        return {"scheduler_run_id": cycle_id, "status": _token(status),
                "materials": materials, "errors": [
                    {"raw_item_id": error["raw_item_id"], "type": _token(error.get("error_type")),
                     "site": _token(error.get("error_site"))}
                    for error in details.get("processing_errors", [])[:5]
                ]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    args = parser.parse_args()
    try:
        client = SSHStateClient(SSHConfig.from_environment(os.environ))
        state, _ = client.read()
        if state.get("status") != "ready" or state.get("run_id") != os.environ["GITHUB_RUN_ID"]:
            raise ValueError("snapshot has not been committed by this workflow")
        with args.snapshot.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != state["snapshot"]["sha256"]:
                raise ValueError("local snapshot differs from committed state")
        observation = observe_cycle(args.snapshot, state["started_at"])
    except Exception as error:
        print(json.dumps({"cycle_observation_error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({"editorial_cycle_observation": observation}))


if __name__ == "__main__":
    main()
