"""История непрерывных проверок источников без свежих сообщений."""

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict

_WARNING_DAYS = {"bvk_outage_news": 14, "media_aero_telegraf": 7}
_VALID_EMPTY = {"empty_index", "empty_fresh", "empty_relevant"}
# Плановый интервал 6 часов. GitHub иногда откладывает плановые запуски.
_MAX_CHECK_GAP = timedelta(hours=36)
_MAX_HISTORY_BYTES = 16_384
_SIGNATURE = re.compile(r"[a-f0-9]{64}")


class _SourceCheck(TypedDict):
    signature: str
    since: str
    checked_at: str


def _saved_check(value: object) -> _SourceCheck | None:
    if not isinstance(value, dict):
        return None
    signature, since, checked_at = (value.get(key) for key in ("signature", "since", "checked_at"))
    if (isinstance(signature, str) and _SIGNATURE.fullmatch(signature)
            and isinstance(since, str) and isinstance(checked_at, str)
            and len(since) <= 50 and len(checked_at) <= 50):
        return {"signature": signature, "since": since, "checked_at": checked_at}
    return None


def _read_history(path: Path) -> dict[str, _SourceCheck]:
    try:
        with path.open("rb") as file:
            content = file.read(_MAX_HISTORY_BYTES + 1)
        if len(content) <= _MAX_HISTORY_BYTES:
            saved = json.loads(content)
            if isinstance(saved, dict) and saved.get("version") == 1:
                sources = saved.get("sources")
                if isinstance(sources, dict):
                    return {
                        key: check for key in _WARNING_DAYS
                        if (check := _saved_check(sources.get(key))) is not None
                    }
    except (OSError, ValueError):
        pass
    return {}


def _previous_start(value: _SourceCheck | None, signature: str, now: datetime) -> datetime:
    if value is not None and value["signature"] == signature:
        try:
            start = datetime.fromisoformat(value["since"])
            observed = datetime.fromisoformat(value["checked_at"])
            if (start.utcoffset() is not None and observed.utcoffset() is not None
                    and start <= observed <= now and now - observed <= _MAX_CHECK_GAP):
                return start.astimezone(UTC)
        except (ValueError, OverflowError):
            pass
    return now


def update_source_silence(rows: list[dict[str, Any]], path: Path, now: datetime) -> bool:
    """Подтверждать тишину только последовательностью успешных чтений."""
    history = _read_history(path)
    for row in rows:
        source_id = row["id"]
        days = _WARNING_DAYS.get(source_id)
        if days is None:
            continue
        previous = history.pop(source_id, None)
        if (row["status"] != "empty" or row.get("item_failures")
                or row.get("empty_reason") not in _VALID_EMPTY):
            continue
        signature = hashlib.sha256(f"{row['url']}\n{row['strategy']}".encode()).hexdigest()
        since = _previous_start(previous, signature, now)
        elapsed = now - since
        row["silence_since"] = since.isoformat()
        row["silence_hours"] = int(elapsed.total_seconds() // 3600)
        row["silence_warning_days"] = days
        if elapsed >= timedelta(days=days):
            row["status"] = "partial"
            row["health_reason"] = "prolonged_silence"
        history[source_id] = {
            "signature": signature, "since": since.isoformat(), "checked_at": now.isoformat(),
        }
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(json.dumps({"version": 1, "sources": history}), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        return False
    return True
