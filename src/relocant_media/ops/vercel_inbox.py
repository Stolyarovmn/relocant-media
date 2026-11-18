"""Bounded private ingress transport; canonical state alone selects its source."""

import json
import re
import time
from contextlib import nullcontext
from typing import Any
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 512 * 1024
TIMEOUT_SECONDS = 15


def ingress_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid ingress origin")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
        or parsed.port is not None or parsed.path not in {"", "/"}
        or parsed.query or parsed.fragment
        or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.vercel\.app",
                            parsed.hostname)
    ):
        raise ValueError("invalid ingress origin")
    return f"https://{parsed.hostname}"


def ingress_config(state: dict[str, Any]) -> dict[str, Any] | None:
    """An existing malformed cutover marker must never restore Telegram polling."""
    if "ingress" not in state:
        return None
    value = state["ingress"]
    if (
        not isinstance(value, dict)
        or not {"version", "backend", "phase", "url", "seed_sha256"} <= set(value)
        or set(value) - {"version", "backend", "phase", "url", "seed_sha256",
                         "seed_snapshot_sha256", "created_at", "activated_at"}
        or type(value.get("version")) is not int or value["version"] != 1
        or value.get("backend") != "vercel" or value.get("phase") not in {"prepared", "active"}
        or not isinstance(value.get("seed_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["seed_sha256"])
    ):
        raise ValueError("invalid canonical ingress configuration")
    if "seed_snapshot_sha256" in value and (
        not isinstance(value["seed_snapshot_sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["seed_snapshot_sha256"])
    ):
        raise ValueError("invalid ingress seed snapshot digest")
    for field in ("created_at", "activated_at"):
        if field in value and not isinstance(value[field], str):
            raise ValueError("invalid ingress audit timestamp")
    ingress_url(value["url"])
    sequence_cursor(state.get("serverless_sequence", 0))
    return value


def sequence_cursor(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid ingress sequence cursor")
    return value


def _request(
    url: str, token: str, payload: dict[str, Any], client: httpx.Client | None,
) -> dict[str, Any]:
    endpoint = ingress_url(url) + "/api/transfer"
    if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
        raise ValueError("ingress authentication required")
    context = (nullcontext(client) if client is not None else httpx.Client(
        timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False,
    ))
    with context as connection:
        deadline = time.monotonic() + TIMEOUT_SECONDS
        with connection.stream(
            "POST", endpoint, json=payload, headers={"Authorization": f"Bearer {token}"},
            timeout=TIMEOUT_SECONDS, follow_redirects=False,
        ) as response:
            response.raise_for_status()
            content = bytearray()
            for part in response.iter_bytes(chunk_size=64 * 1024):
                if time.monotonic() > deadline:
                    raise TimeoutError("ingress response deadline exceeded")
                content.extend(part)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise ValueError("ingress response exceeds bound")
    result = json.loads(content)
    if not isinstance(result, dict) or result.get("status") != "ready":
        raise ValueError("ingress is not ready")
    return result


def peek_inbox(
    url: str, token: str, after: int, limit: int = 30, *, client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    sequence_cursor(after)
    if type(limit) is not int or not 1 <= limit <= 30:
        raise ValueError("invalid ingress batch limit")
    value = _request(url, token, {"operation": "peek", "after": after, "limit": limit}, client)
    events = value.get("events")
    if not isinstance(events, list) or len(events) > limit:
        raise ValueError("invalid ingress batch")
    for index, event in enumerate(events, start=after + 1):
        if (not isinstance(event, dict) or type(event.get("sequence")) is not int
                or event["sequence"] != index):
            raise ValueError("noncontiguous ingress sequence")
    return events


def acknowledge_inbox(
    url: str, token: str, through: int, snapshot_sha256: str, *,
    open_drafts: list[dict[str, Any]] | None = None,
    client: httpx.Client | None = None,
) -> None:
    sequence_cursor(through)
    if not re.fullmatch(r"[0-9a-f]{64}", snapshot_sha256):
        raise ValueError("invalid ingress checkpoint digest")
    payload: dict[str, Any] = {"operation": "ack", "through": through,
                               "snapshot_sha256": snapshot_sha256}
    if open_drafts is not None:
        payload["open_drafts"] = open_drafts
    _request(url, token, payload, client)
