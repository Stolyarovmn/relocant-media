"""Offline preparation of an exact-CAS recovery request; never runs a worker or sends."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from relocant_media.ops.actions_runtime import validate_ready
from relocant_media.ops.sqlite_restore import restore
from relocant_media.ops.ssh_state_client import _state_digest


def prepare(checkpoint: dict[str, Any], busy: dict[str, Any], snapshot: Path,
            output: Path, *, writers_fenced: bool) -> dict[str, Any]:
    """Verify the complete checkpoint, including SQLite, before preparing a request.

    The operator must independently fence writers. This attestation is deliberately
    not inferred from elapsed time or a completed Actions job.
    """
    if not writers_fenced:
        raise ValueError("fence all writers before preparing recovery")
    expected = _state_digest(busy)
    state = checkpoint.get("state")
    if (checkpoint.get("version") != 1 or busy.get("status") != "busy"
            or checkpoint.get("expected_busy_sha256") != expected
            or not isinstance(state, dict)):
        raise ValueError("checkpoint does not match current busy pointer")
    validate_ready(state)
    if (state.get("attempt") != busy.get("attempt")
            or state.get("generation") != busy.get("attempt")
            or state.get("run_id") != busy.get("run_id")
            or state.get("run_attempt") != busy.get("run_attempt")
            or state.get("cutover") != busy.get("cutover")
            or type(state.get("last_exit_code")) is not int
            or state["last_exit_code"] < 0
            or not isinstance(state.get("intake_state"), dict)):
        raise ValueError("checkpoint identity or child outcome is incomplete")
    output.mkdir(parents=True, exist_ok=False)
    manifest_path = output / "manifest.json"
    manifest_path.write_text(json.dumps(state["snapshot"]), encoding="utf-8")
    restore(snapshot, manifest_path, output / "verified")
    request = {"op": "write_state", "expected_sha": expected, "state": state}
    (output / "request.json").write_text(json.dumps(request, sort_keys=True,
        ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    audit = {"expected_busy_sha256": expected,
             "snapshot_sha256": state["snapshot"]["sha256"],
             "request_sha256": hashlib.sha256((output / "request.json").read_bytes()).hexdigest(),
             "writers_fenced_attestation": True, "applied": False}
    (output / "audit.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    return audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--busy-state", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--writers-fenced", action="store_true")
    args = parser.parse_args()
    audit = prepare(json.loads(args.checkpoint.read_text(encoding="utf-8")),
                    json.loads(args.busy_state.read_text(encoding="utf-8")),
                    args.snapshot, args.output, writers_fenced=args.writers_fenced)
    print(json.dumps(audit))


if __name__ == "__main__":
    main()
