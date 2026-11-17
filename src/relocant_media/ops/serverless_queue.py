"""Read the private Serverless queue with the version-pinned Telegram CLI."""

import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

_CLI = "@tgcloud/cli@0.2.0"


def decode_result(output: str) -> dict[str, Any]:
    line = next((line for line in reversed(output.splitlines()) if line.startswith("< ")), "")
    if not line:
        raise ValueError("missing Serverless CLI result")
    value, end = json.JSONDecoder().raw_decode(line[2:])
    timing = line[2 + end:].strip()
    if not isinstance(value, dict) or (timing and not timing.removesuffix("ms").isdigit()):
        raise ValueError("invalid Serverless CLI result")
    return value


def peek_inbox(project: Path, token: str, after: int, *, limit: int = 30) -> list[dict[str, Any]]:
    if not token or type(after) is not int or after < 0:
        raise ValueError("Serverless CLI access and a valid checkpoint are required")
    environment = {
        key: os.environ[key] for key in ("PATH", "HOME", "TEMP", "TMP", "SystemRoot")
        if key in os.environ
    }
    environment.update(TGCLOUD_TOKEN=token, NO_COLOR="1", CI="true")
    with TemporaryDirectory(prefix="relocant-tgcloud-") as scratch:
        endpoint = Path(scratch) / "tgcloud" / "endpoints"
        endpoint.mkdir(parents=True)
        shutil.copyfile(project / "tools" / "transfer.js", endpoint / "transfer.js")
        response = subprocess.run(
            [
                "npx", "--yes", "--package", _CLI, "tgcloud", "run", "endpoints/transfer",
                json.dumps({"operation": "peek", "limit": limit, "after": after}),
                "--ctx", '{"operator":"relocant-intake"}',
            ],
            cwd=scratch, env=environment, capture_output=True, encoding="utf-8", timeout=60,
        )
    if response.returncode:
        raise RuntimeError("Telegram Serverless queue is unavailable")
    result = decode_result(response.stdout)
    events = result.get("events")
    if result.get("status") != "ready" or not isinstance(events, list) or len(events) > limit:
        raise ValueError("invalid Serverless queue result")
    if any(not isinstance(entry, dict) for entry in events):
        raise ValueError("invalid Serverless queue envelope")
    return cast(list[dict[str, Any]], events)
