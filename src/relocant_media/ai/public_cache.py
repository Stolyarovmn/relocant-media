"""Short-lived exact preparation cache for explicitly public news only."""
import hashlib
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from relocant_media.ai.package import canonical_json
from relocant_media.ai.provider import StructuredAIProvider, StructuredOutputError

_TASKS = frozenset({"rank", "claims", "verify", "draft"})
_TTL = timedelta(hours=4)


class PublicPreparationCache:
    def __init__(
        self, provider: StructuredAIProvider, directory: Path, *, model_id: str,
        public_news_only: bool = False,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        audit: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if public_news_only is not True:
            raise ValueError("cache requires explicit public-news-only scope")
        if not model_id.strip():
            raise ValueError("cache model id is required")
        self.provider, self.directory, self.model_id = provider, Path(directory), model_id
        self.now, self.audit = now, audit

    def _time(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("cache clock must be timezone-aware")
        return value.astimezone(UTC)

    def _load(self, path: Path, key: str, now: datetime) -> str | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if (not isinstance(payload, dict) or payload.get("key") != key
                    or not isinstance(payload.get("created_at"), str)):
                return None
            created = datetime.fromisoformat(payload["created_at"])
            if created.tzinfo is None or not timedelta(0) <= now - created <= _TTL:
                return None
            response = payload.get("response")
            if not isinstance(response, str) or not response.strip():
                return None
            return response
        except (OSError, ValueError, TypeError):
            return None

    def _save(self, path: Path, key: str, response: str, now: datetime) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".preparation-", dir=self.directory)
        try:
            os.chmod(temporary, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"key": key, "created_at": now.isoformat(), "response": response},
                          stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def generate_json(self, *, task: str, system_instruction: str, prompt: str,
                      response_schema: dict[str, Any]) -> str:
        arguments = dict(task=task, system_instruction=system_instruction,
                         prompt=prompt, response_schema=response_schema)
        if task not in _TASKS:
            return self.provider.generate_json(task=task, system_instruction=system_instruction,
                prompt=prompt, response_schema=response_schema)
        now = self._time()
        key = hashlib.sha256(canonical_json({
            **arguments, "model_id": self.model_id,
            "belgrade_day": now.astimezone(ZoneInfo("Europe/Belgrade")).date().isoformat(),
        }).encode("utf-8")).hexdigest()
        path = self.directory / f"{key}.json"
        cached = self._load(path, key, now)
        if cached is not None:
            if self.audit is not None:
                self.audit({"task": task, "cached": True, "key": key})
            return cached
        response = self.provider.generate_json(task=task, system_instruction=system_instruction,
            prompt=prompt, response_schema=response_schema)
        if not isinstance(response, str) or not response.strip():
            raise StructuredOutputError("preparation provider returned empty or non-text output")
        self._save(path, key, response, now)
        if self.audit is not None:
            self.audit({"task": task, "cached": False, "key": key})
        return response
