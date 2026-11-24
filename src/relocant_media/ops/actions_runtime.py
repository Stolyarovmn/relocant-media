"""Hosted compute with immutable SSH/Drive backups and a fail-closed CAS state pointer.

No automatic bootstrap, lock expiry, send retries, schema creation, or fallback DB.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Any, Protocol

import google.auth
import httpx
from google.auth.transport.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ai.package import EditorialPackage, canonical_json
from relocant_media.ai.quota_diagnostics import safe_quota
from relocant_media.ops.belgrade_digests import (
    DIGEST_ATTEMPT_KEYS,
    DIGEST_SKIP_REASONS,
    digest_delivery,
    morning_delivery,
    public_digest_days,
)
from relocant_media.ops.belgrade_digests import due_slot as due_digest_slot
from relocant_media.ops.belgrade_digests import run as run_digest
from relocant_media.ops.digest_preview import preview as preview_digest
from relocant_media.ops.events_export import export_events_snapshot
from relocant_media.ops.gpt_export_receipt import (
    EXPORT_KIND,
    PUBLIC_SOURCE_TYPES,
    require_public_documents,
    validate_export_marker,
)
from relocant_media.ops.gpt_submission import GPTSubmission, verified_submission
from relocant_media.ops.map_backfill import apply as apply_map_buttons
from relocant_media.ops.map_backfill import parse_targets as parse_map_button_targets
from relocant_media.ops.map_export import export_map_snapshot
from relocant_media.ops.map_republication import GPTReviews as MapGPTReviews
from relocant_media.ops.map_republication import Request as MapRepublicationRequest
from relocant_media.ops.map_republication import apply as apply_map_republication
from relocant_media.ops.map_republication import resolve_previous as resolve_map_republication
from relocant_media.ops.map_republication import review_request as map_review_request
from relocant_media.ops.newsroom_merge import merge
from relocant_media.ops.serverless_snapshot import open_draft_routes
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.ops.sqlite_restore import restore
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient, _state_digest
from relocant_media.ops.vercel_inbox import acknowledge_inbox, ingress_config, sequence_cursor
from relocant_media.services.cloud_discovery import batch_receipt, ingest_batch, parse_batch
from relocant_media.services.material_lifecycle import can_admit_queued_fallback, sweep_materials

REPO = "Stolyarovmn/relocant-media"
STATE_BRANCH = "editorial-state"
STATE_PATH = "state/current.json"
MAX_BYTES = 256 * 1024 * 1024
SNAPSHOT_GROWTH_RESERVE = 16 * 1024 * 1024
PUBLIC_EXPORT_TIMEOUT_SECONDS = 180
GPT_EXCHANGE_MAX_BYTES = 512 * 1024
BELGRADE_STREETS = Path("cities/belgrade/map/roads.json.gz")
BELGRADE_AREAS = Path("cities/belgrade/map/outage-areas.json.gz")
MODES = {
    "preview", "editorial", "intake", "discovery", "auto", "fallback-preview", "fallback",
    "map-backfill", "map-republish", "map-republish-review", "digest", "digest-preview",
    "drain", "drain-close",
}


@dataclass(frozen=True)
class _DrainOptions:
    before: datetime
    max_attempts: int
    max_seconds: int
    runs_left: int
    close_existing: bool = False


def _drain_options(environment: Mapping[str, str], *, now: datetime,
                   close_existing: bool = False) -> _DrainOptions:
    """Проверить ручные границы до восстановления и CAS-захвата писателя."""
    value = environment.get("DRAIN_BEFORE", "")
    if (not isinstance(value, str) or not 1 <= len(value) <= 64 or "T" not in value
            or not value.endswith(("Z", "+00:00"))):
        raise ValueError("Нужен ISO cutoff в UTC")
    try:
        before = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Нужен ISO cutoff в UTC") from None
    if (before.tzinfo is None or before.utcoffset() != timedelta()
            or now.tzinfo is None or now.utcoffset() is None or before > now):
        raise ValueError("Cutoff должен быть UTC и не позднее текущего времени")

    def limit(key: str, default: int, maximum: int) -> int:
        raw = environment.get(key, "") or str(default)
        if not isinstance(raw, str) or not re.fullmatch(r"[1-9][0-9]{0,3}", raw):
            raise ValueError("Неверный лимит drain")
        number = int(raw)
        if number > maximum:
            raise ValueError("Превышен лимит drain")
        return number

    return _DrainOptions(before.astimezone(UTC), limit("DRAIN_MAX_ATTEMPTS", 10, 30),
                         limit("DRAIN_MAX_SECONDS", 300, 900),
                         limit("DRAIN_RUNS_LEFT", 1, 200), close_existing)


def _drain_control(run_dir: Path, options: _DrainOptions, code: int) -> tuple[bool, int]:
    """Закрытый отчёт не попадает в stdout, публичную проекцию или GITHUB_OUTPUT."""
    if code != 0:
        return False, 0
    path = run_dir / "drain-report.json"
    if path.stat().st_size > 256 * 1024:
        raise ValueError("Неверный отчёт drain")
    report = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(report, dict) or report.get("status") != "DRAIN_OK"
            or type(report.get("remaining")) is not int or report["remaining"] < 0
            or type(report.get("needs_continue")) is not bool
            or report["needs_continue"] and report["remaining"] == 0
            or type(report.get("next_delay_seconds")) is not int
            or not 0 <= report["next_delay_seconds"] <= 60):
        raise ValueError("Неверный отчёт drain")
    if options.close_existing:
        if (report.get("mode") != "close_existing" or report.get("closed_by_owner") is not True
                or report["remaining"] != 0 or report["needs_continue"]):
            raise ValueError("Закрытие старой очереди не подтверждено")
        return False, 0
    next_at = report.get("next_at")
    delay = max(60, report["next_delay_seconds"])
    if next_at is not None:
        if not isinstance(next_at, str) or len(next_at) > 64:
            raise ValueError("Неверный следующий срок drain")
        instant = datetime.fromisoformat(next_at)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Неверный следующий срок drain")
        # Job продолжения ограничен шестью часами; оставить десять минут на старт и dispatch.
        delay = min(21000, max(delay, ceil((instant-datetime.now(UTC)).total_seconds())))
    should_continue = (report["remaining"] > 0 and report["needs_continue"]
                       and options.runs_left > 1)
    return should_continue, delay if should_continue else 0


def _write_drain_control(environment: Mapping[str, str], control: tuple[bool, int]) -> None:
    should_continue, delay = control
    if (type(should_continue) is not bool or type(delay) is not int
            or not 0 <= delay <= 21000 or should_continue and delay < 60
            or not should_continue and delay != 0):
        raise ValueError("Неверные границы продолжения drain")
    if output := environment.get("GITHUB_OUTPUT"):
        with Path(output).open("a", encoding="utf-8") as stream:
            stream.write(f"drain_continue={str(should_continue).lower()}\n")
            stream.write(f"drain_next_delay_seconds={delay}\n")


def _write_digest_watch(
    state: dict[str, Any], environment: Mapping[str, str], *, now: datetime,
) -> None:
    output = environment.get("GITHUB_OUTPUT")
    if (not output or environment.get("GITHUB_ACTIONS") != "true"
            or environment.get("EDITORIAL_SEND_ENABLED") != "true"
            or environment.get("EDITORIAL_CHANNEL_MODE") != "public"):
        return
    try:
        alerts = [delivery for delivery in (
            morning_delivery(state, now), digest_delivery(state, now, "evening"),
        ) if delivery is not None and delivery.needs_alert]
        if alerts:
            with Path(output).open("a", encoding="utf-8") as stream:
                for delivery in alerts:
                    slot = delivery.slot
                    stream.write(f"{slot.kind}_watch_date={slot.scheduled.date()}\n")
    except (OSError, ValueError) as error:
        print(json.dumps({"digest_watch_error": type(error).__name__}), file=sys.stderr)


def _sweep_material_lifecycle(database: Path, *, now: datetime | None = None) -> bool:
    """Только канонический писатель, после CAS; без моделей и сетевых действий."""
    engine = create_engine(f"sqlite:///{database.resolve().as_posix()}")
    try:
        with Session(engine) as session:
            sweep_materials(session, city="belgrade", now=now or datetime.now(UTC))
            # This engine is fresh and used only for this sweep.
            result = session.connection().exec_driver_sql("SELECT total_changes()")
            changes = int(result.scalar_one())
            session.commit()
            return changes > 0
    finally:
        engine.dispose()


def _gpt_digest(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("private GPT exchange digest required")
    return value


def _gpt_blob(storage: SnapshotStore, digest: str) -> dict[str, Any]:
    content = storage.download(digest)
    if len(content) > GPT_EXCHANGE_MAX_BYTES or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("invalid private GPT exchange blob")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("private GPT exchange object required")
    return value


def _prepare_gpt_input(storage: SnapshotStore, digest: str, path: Path) -> bool:
    from relocant_media.services.gpt_refresh import RefreshResult

    result = _gpt_blob(storage, digest)
    packages = result.get("packages")
    rejected = result.get("rejected", [])
    deferred = result.get("deferred", [])
    package_result = (
        set(result) - {"rejected", "deferred"} == {"version", "request_sha256", "packages"}
        and type(result["version"]) is int and result["version"] == 1
        and isinstance(packages, list) and isinstance(rejected, list)
        and isinstance(deferred, list) and 1 <= len(packages) + len(rejected) + len(deferred) <= 5
    )
    rank_result = (
        set(result) == {"version", "kind", "request_sha256", "selected_ids"}
        and type(result["version"]) is int and result["version"] == 2
        and result["kind"] == "batch_preselection"
        and isinstance(result["selected_ids"], list) and len(result["selected_ids"]) <= 5
    )
    refresh_result = result.get("kind") == "evidence_refresh"
    if refresh_result:
        RefreshResult.model_validate(result)
    if not package_result and not rank_result and not refresh_result:
        raise ValueError("invalid GPT result envelope")
    request_sha = _gpt_digest(result["request_sha256"])
    request = _gpt_blob(storage, request_sha)
    if hashlib.sha256(canonical_json(request).encode()).hexdigest() != request_sha:
        raise ValueError("GPT request blob is not canonical")
    if (rank_result or refresh_result) and hashlib.sha256(
        canonical_json(result).encode()
    ).hexdigest() != digest:
        raise ValueError("GPT result blob is not canonical")
    path.write_text(canonical_json({"request": request, "result": result}), encoding="utf-8")
    return rank_result or refresh_result or not packages


def _upload_gpt_request(storage: SnapshotStore, request: dict[str, Any], path: Path) -> str:
    content = canonical_json(request).encode()
    if len(content) > GPT_EXCHANGE_MAX_BYTES:
        raise ValueError("GPT request exceeds private exchange bound")
    path.write_bytes(content)
    digest = storage.upload(path, "gpt-fallback-request")
    if digest != hashlib.sha256(content).hexdigest():
        raise ValueError("GPT request storage receipt differs")
    return digest


def _export_gpt_request(
    storage: SnapshotStore, database: Path, work: Path, scheduler_run_id: int | None = None,
) -> int:
    from relocant_media.services.gpt_batch_fallback import (
        build_batch_request,
        build_ranked_request,
        validate_batch_handoff,
    )
    from relocant_media.services.gpt_fallback import (
        FallbackValidationError,
        _recent_runs,
        build_request,
    )

    engine = create_engine(f"sqlite:///{database.resolve().as_posix()}")
    try:
        with Session(engine) as session:
            now = datetime.now(UTC)
            request = None
            for run in _recent_runs(session, now):
                if scheduler_run_id is not None and run.id != scheduler_run_id:
                    continue
                batch = run.details.get("batch_preselection")
                if not isinstance(batch, dict) or batch.get("status") != "gpt_ranked":
                    continue
                if not batch.get("gpt_selected_ids"):
                    continue
                try:
                    validate_batch_handoff(run, now=now)
                except FallbackValidationError:
                    continue
                batch_request = _gpt_blob(storage, _gpt_digest(batch.get("gpt_request_sha256")))
                batch_result = _gpt_blob(storage, _gpt_digest(batch.get("gpt_rank_sha256")))
                request = build_ranked_request(session, batch_request, batch_result, now=now)
                if request is not None:
                    break
            if request is None:
                request = build_batch_request(session, now=now, scheduler_run_id=scheduler_run_id)
            if request is None:
                request = build_request(session, now=now, scheduler_run_id=scheduler_run_id)
            deadlines = (_request_deadlines(session, request)
                         if request is not None else [])
    finally:
        engine.dispose()
    if request is None:
        print(json.dumps({"gpt_fallback": "no_pending_candidates"}))
        return 0
    known = [deadline for deadline in deadlines if deadline is not None]
    respond_by = min(known) if known else None
    if known and len(known) == len(deadlines) and max(known) - now < GPT_RESPONSE_MIN_MARGIN:
        # Ответ на такой запрос будет отклонён как просроченный; не тратим подписку.
        print(json.dumps({"gpt_fallback": "deadline_too_close",
                          "respond_by": max(known).isoformat()}))
        return 0
    digest = _upload_gpt_request(storage, request, work / "gpt-request.json")
    print(json.dumps({"gpt_fallback_request_sha256": digest,
                      "candidate_count": len(request["candidates"]),
                      "respond_by": respond_by.isoformat() if respond_by else None}))
    return 0


# Запрос без запаса времени почти наверняка опоздает: резервное окно материала 30 минут.
GPT_RESPONSE_MIN_MARGIN = timedelta(minutes=10)


def _request_deadlines(session: Session, request: dict[str, Any]) -> list[datetime | None]:
    """Срок по каждому кандидату; None — окно откроется только при импорте."""
    from relocant_media.services.material_lifecycle import response_deadline

    deadlines: list[datetime | None] = []
    for candidate in request.get("candidates", []):
        raw_id = candidate.get("raw_item_id") if isinstance(candidate, dict) else None
        deadlines.append(response_deadline(session, raw_id)
                         if isinstance(raw_id, int) and 0 < raw_id < 2**63 else None)
    return deadlines


def _gpt_result_expired(
    database: Path, exchange_file: Path, now: datetime, submission: GPTSubmission | None = None,
) -> bool:
    """Определить подлинную просрочку ответа до получения права записи."""
    from relocant_media.ops.gpt_fallback_worker import FallbackInput
    from relocant_media.services.gpt_fallback import obsolete_fallback_response

    try:
        payload = FallbackInput.model_validate_json(exchange_file.read_bytes())
    except ValueError:
        return False
    engine = create_engine(f"sqlite:///{database.resolve().as_posix()}")
    try:
        with Session(engine) as session, session.no_autoflush:
            session.connection().exec_driver_sql("PRAGMA query_only=ON")

            def expired(raw_id: int, package: EditorialPackage | None = None,
                        *, final: bool) -> bool:
                return (obsolete_fallback_response(
                    session, payload.request, raw_id, now=now, package=package,
                ) == "material_expired" and (
                    not final or submission is None or not can_admit_queued_fallback(
                        session, raw_id, issued=payload.request.get("created_at"),
                        received=submission.received_at, now=now,
                    )
                ))

            return (all(expired(pkg.raw_item_id, pkg, final=True)
                        for pkg in payload.result.packages)
                    and all(expired(item.raw_item_id, final=True)
                            for item in payload.result.rejected)
                    and all(expired(item.raw_item_id, final=False)
                            for item in payload.result.deferred))
    finally:
        engine.dispose()


def _runtime_phase(phase: str) -> None:
    """Только фиксированные имена стадий; никаких исходных текстов и реквизитов."""
    print(json.dumps({"runtime_phase": phase}), file=sys.stderr, flush=True)


def _require_public_fallback_documents(session: Session, request: dict[str, Any]) -> None:
    """Реестр сам по себе не даёт закрытому источнику право внешнего экспорта."""
    from relocant_media.ai.package import PackageEventDocument
    from relocant_media.services.editorial import _snapshot_member
    from relocant_media.services.gpt_fallback import _registered_public, _registry
    from relocant_media.storage.models import RawItem, Source

    require_public_documents(request)
    registry = _registry()
    from relocant_media.ai.linked_update import PublishedPostReference
    from relocant_media.services.linked_updates import published_post_reference

    for value in request.get("published_post_refs", []):
        reference = PublishedPostReference.model_validate_json(canonical_json(value))
        current = published_post_reference(
            session, reference.raw_item_id, city="belgrade", now=datetime.now(UTC),
        )
        if current != reference:
            raise ValueError("Прежняя публикация не соответствует канонической квитанции")
    for candidate in request["candidates"]:
        for snapshot in candidate["documents"]:
            document = PackageEventDocument.model_validate_json(canonical_json(snapshot))
            item = session.get(RawItem, document.raw_item_id)
            source = session.get(Source, document.source_id)
            config = registry.get(document.source_id)
            if (document.ownership not in {"official", "media"} or item is None or source is None
                    or item.source_id != source.id or config is None
                    or config.ownership not in {"official", "media"}
                    or config.source_type not in PUBLIC_SOURCE_TYPES
                    or not _registered_public(item, source, config)
                    or canonical_json(snapshot) != canonical_json(_snapshot_member(item, source))):
                raise ValueError("Внешний экспорт допускает только точные публичные документы")


def _export_committed_fallback(
    storage: SnapshotStore, database: Path, work: Path, *, ready: dict[str, Any],
    mode: str, environment: Mapping[str, str],
) -> None:
    """Опциональный public-only v1 на уже сохранённой локальной БД, без нового restore."""
    from relocant_media.services.gpt_fallback import build_request

    preferred_cycle = None
    if mode == "editorial":
        try:
            report = json.loads((work / "cycle" / "server-report.json").read_text(
                encoding="utf-8",
            ))
        except (OSError, ValueError):
            report = None
        if isinstance(report, dict) and report.get("status") in ("PRIMARY_OK", "PRIMARY_PARTIAL"):
            run_id = report.get("run_id")
            if type(run_id) is int and 0 < run_id < 2**63:
                preferred_cycle = run_id

    now = datetime.now(UTC)
    engine = create_engine(f"sqlite:///{database.resolve().as_posix()}")
    try:
        with Session(engine) as session, session.no_autoflush:
            session.connection().exec_driver_sql("PRAGMA query_only=ON")
            request = (build_request(session, now=now, scheduler_run_id=preferred_cycle)
                       if preferred_cycle is not None else None)
            if request is None:
                request = build_request(session, now=now)
            if request is not None:
                _require_public_fallback_documents(session, request)
    finally:
        engine.dispose()
    if request is None:
        print(json.dumps({"gpt_fallback_export_status": "no_pending_candidates"}), flush=True)
        return
    # Валидация контекста до upload; ни локальная подделка, ни другой job не анонсируются.
    marker = validate_export_marker({
        "gpt_fallback_export": EXPORT_KIND,
        "gpt_fallback_request_sha256": hashlib.sha256(canonical_json(request).encode()).hexdigest(),
        "candidate_count": len(request["candidates"]),
        "scheduler_run_id": request["scheduler_run_id"],
        "snapshot_sha256": ready["snapshot"]["sha256"], "generation": ready["generation"],
        "workflow_run_id": int(environment.get("GITHUB_RUN_ID", "")),
        "workflow_run_attempt": int(environment.get("GITHUB_RUN_ATTEMPT", "")),
        "head_sha": environment.get("GITHUB_SHA"), "job_key": environment.get("GITHUB_JOB"),
        "worker_mode": mode,
    })
    if type(request.get("version")) is not int or request["version"] != 1:
        raise ValueError("Канонический экспорт допускает только существующий запрос v1")
    digest = _upload_gpt_request(storage, request, work / "gpt-committed-request.json")
    if digest != marker["gpt_fallback_request_sha256"]:
        raise ValueError("Квитанция upload не совпадает с каноническим запросом")
    print(json.dumps(marker), flush=True)


class SnapshotCapacityError(ValueError):
    """Insufficient bounded storage headroom; raised before claiming the writer."""


def require_snapshot_capacity(database: Path) -> None:
    if database.stat().st_size > MAX_BYTES - SNAPSHOT_GROWTH_RESERVE:
        raise SnapshotCapacityError("snapshot capacity reserve exhausted")



def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", value):
        raise ValueError("invalid Drive identifier")
    return value


def repository_name(value: str) -> str:
    if (not isinstance(value, str) or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}", value)
            or value.rsplit("/", 1)[-1] in {".", ".."}):
        raise ValueError("GitHub state repository must be owner/name")
    return value


class Drive:
    def __init__(self, client: httpx.Client) -> None:
        self.client = client
        scopes = ["https://www.googleapis.com/auth/drive.file"]
        if os.environ.get("DRIVE_CREDENTIALS_JSON"):
            self.credentials, _ = google.auth.load_credentials_from_dict(  # type: ignore[no-untyped-call]
                json.loads(os.environ["DRIVE_CREDENTIALS_JSON"]), scopes=scopes)
        else:
            self.credentials, _ = google.auth.default(scopes=scopes)

    def headers(self) -> dict[str, str]:
        if not self.credentials.valid:
            self.credentials.refresh(Request())
        return {"Authorization": f"Bearer {self.credentials.token}"}

    def download(self, file_id: str) -> bytes:
        with self.client.stream(
            "GET", f"https://www.googleapis.com/drive/v3/files/{identifier(file_id)}",
            params={"alt": "media", "supportsAllDrives": "true"}, headers=self.headers(),
        ) as response:
            response.raise_for_status()
            result = bytearray()
            for chunk in response.iter_bytes():
                result.extend(chunk)
                if len(result) > MAX_BYTES:
                    raise ValueError("Drive snapshot exceeds bound")
            return bytes(result)

    def upload(self, path: Path, folder: str) -> str:
        content = path.read_bytes()
        if len(content) > MAX_BYTES:
            raise ValueError("Drive snapshot exceeds bound")
        boundary = uuid.uuid4().hex
        metadata = json.dumps({"name": f"actions-{uuid.uuid4().hex}-editorial.sqlite3",
                               "parents": [identifier(folder)]}).encode()
        body = (f"--{boundary}\r\nContent-Type: application/json\r\n\r\n".encode()
                + metadata
                + f"\r\n--{boundary}\r\nContent-Type: application/vnd.sqlite3\r\n\r\n".encode()
                + content + f"\r\n--{boundary}--\r\n".encode())
        response = self.client.post(
            "https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id"},
            headers={**self.headers(), "Content-Type": f"multipart/related; boundary={boundary}"},
            content=body,
        )
        response.raise_for_status()
        file_id = identifier(response.json()["id"])
        if hashlib.sha256(self.download(file_id)).digest() != hashlib.sha256(content).digest():
            raise ValueError("Drive readback differs; state remains busy")
        return file_id


class StateStore:
    """Contents SHA is the compare-and-swap token; branch code is never executed."""

    def __init__(self, client: httpx.Client, token: str, repository: str = REPO) -> None:
        if not token:
            raise ValueError("GitHub state credential required")
        self.repository = repository_name(repository)
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json"}
        self.base_url = f"https://api.github.com/repos/{self.repository}"
        self.url = f"{self.base_url}/contents/{STATE_PATH}"

    def require_private(self) -> dict[str, Any]:
        # A data branch in a public repository is public too. Never infer privacy
        # from the workflow environment, token permissions, or a previous check.
        response = self.client.get(self.base_url, headers=self.headers)
        response.raise_for_status()
        repository = response.json()
        if (not isinstance(repository, dict)
                or repository.get("full_name") != self.repository
                or repository.get("private") is not True):
            raise ValueError("private repository required for canonical editorial state")
        return repository

    def read(self) -> tuple[dict[str, Any], str]:
        self.require_private()
        response = self.client.get(self.url, params={"ref": STATE_BRANCH}, headers=self.headers)
        response.raise_for_status()
        result = response.json()
        state = json.loads(base64.b64decode(result["content"]))
        if not isinstance(state, dict):
            raise ValueError("invalid control record")
        return state, str(result["sha"])

    def write(self, state: dict[str, Any], sha: str) -> str:
        self.require_private()
        response = self.client.put(self.url, headers=self.headers, json={
            "message": "Checkpoint hosted editorial state", "branch": STATE_BRANCH, "sha": sha,
            "content": base64.b64encode(json.dumps(state).encode()).decode(),
        })
        response.raise_for_status()
        return str(response.json()["content"]["sha"])



class CanonicalStore(Protocol):
    def read(self) -> tuple[dict[str, Any], str]: ...

    def write(self, state: dict[str, Any], sha: str) -> str: ...


class SnapshotStore(Protocol):
    def download(self, file_id: str) -> bytes: ...

    def upload(self, path: Path, folder: str) -> str: ...


def build_storage(
    client: httpx.Client, environment: Mapping[str, str], *, bootstrap: bool = False,
    drive_factory: Callable[[httpx.Client], SnapshotStore] | None = None,
) -> tuple[CanonicalStore, SnapshotStore, str]:
    backend = environment.get("EDITORIAL_STORAGE_BACKEND", "drive")
    if backend == "ssh":
        storage = SSHStateClient(SSHConfig.from_environment(environment))
        return storage, storage, "ssh_blobs"
    if backend != "drive":
        raise ValueError("EDITORIAL_STORAGE_BACKEND must be ssh or drive")
    store = StateStore(client, environment.get("GH_STATE_TOKEN", ""),
                       repository=environment.get("GH_STATE_REPOSITORY") or REPO)
    if bootstrap:
        store.require_private()
    drive = (drive_factory or Drive)(client)
    return store, drive, environment.get("DRIVE_STATE_FOLDER_ID", "")


def validate_ready(state: dict[str, Any]) -> None:
    if (state.get("version") != 1 or state.get("owner") != "github-actions"
            or state.get("status") != "ready" or not state.get("generation")
            or state.get("cutover", {}).get("server_fenced") is not True):
        raise ValueError("canonical state not ready; never reset a busy marker automatically")
    manifest = state["snapshot"]
    identifier(manifest["drive_file_id"])
    if not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]):
        raise ValueError("snapshot digest required")


def restored(drive: SnapshotStore, manifest: dict[str, Any], work: Path) -> Path:
    content = drive.download(manifest["drive_file_id"])
    source = work / "download.sqlite3"
    source.write_bytes(content)
    manifest_path = work / "download-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    restore(source, manifest_path, work / "database")
    return work / "database" / "editorial.sqlite3"


def run_child(command: list[str], environment: dict[str, str]) -> int:
    # Killing only the immediate Python process can leave a Gemini/worker descendant alive.
    # Early failures print a report before an output directory exists. Keep raw stdout
    # private and ephemeral; expose only the existing coordinate allowlist on failure.
    with tempfile.TemporaryFile() as captured:
        child = subprocess.Popen(command, env=environment, start_new_session=True,
                                 stdout=captured, stderr=subprocess.DEVNULL)
        try:
            code = child.wait(timeout=4100)
        except BaseException:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            raise
        if code > 0:
            captured.seek(0, os.SEEK_END)
            captured.seek(max(0, captured.tell() - 65536))
            for line in reversed(captured.read().splitlines()):
                try:
                    report = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(report, dict) and report.get("status") in (
                    "SERVER_FAILED", "PRIMARY_FAILED",
                ):
                    print_failure_coordinates(report)
                    break
        return code


def _processing_error_coordinates(errors: object) -> list[dict[str, str]]:
    coordinates = []
    for error in errors[:5] if isinstance(errors, list) else []:
        if not isinstance(error, dict):
            continue
        safe = {key: value for key in ("stage", "error_type", "error_site", "reason")
                if isinstance(value := error.get(key), str)
                and re.fullmatch(r"[A-Za-z0-9_.:]{1,80}", value)}
        if safe:
            coordinates.append(safe)
    return coordinates


def print_failure_coordinates(report: dict[str, Any]) -> None:
    fields: dict[str, Any] = {
        key: value for key in ("status", "error_type", "error_site", "backup_error_type")
              if isinstance(value := report.get(key), str)
              and re.fullmatch(r"[A-Za-z0-9_.:]{1,80}", value)}
    coordinates = _processing_error_coordinates(report.get("processing_errors"))
    if coordinates:
        fields["processing_errors"] = coordinates
    print(json.dumps({"runtime_failure": fields}), file=sys.stderr)


def report_failure(run_dir: Path, code: int) -> None:
    """Child output stays hidden; print only whitelisted failure coordinates to stderr."""
    if code == 0:
        return
    try:
        report = json.loads((run_dir / "server-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(report, dict):
        print_failure_coordinates(report)


def _counters(value: object, keys: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in keys if type(value.get(key)) is int}


def _report_fallback_result(run_dir: Path) -> None:
    from relocant_media.services.gpt_fallback import FallbackDeferredReason

    try:
        report = json.loads((run_dir / "server-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(report, dict) or report.get("mode") != "subscription_gpt_fallback":
        return
    status = report.get("status")
    if status in ("PRIMARY_OK", "PRIMARY_PARTIAL"):
        counts = _counters(report, (
            "imported", "declined", "rejected", "unresolved", "awaiting_owner",
            "sent_count", "delivery_blocked",
        ))
        result: dict[str, Any] = {"status": status}
        result.update({key: count for key, count in counts.items() if 0 <= count <= 5})
        deferred = _counters(report.get("deferred_reasons"), tuple(FallbackDeferredReason))
        if safe_deferred := {key: count for key, count in deferred.items() if 1 <= count <= 5}:
            result["deferred_reasons"] = safe_deferred
        errors = _processing_error_coordinates(report.get("processing_errors"))
        if errors:
            result["processing_errors"] = errors
        print(json.dumps({"gpt_fallback_result": result}), flush=True)

    counters = _counters(report.get("obsolete_responses"), (
        "already_resolved", "attempt_in_progress", "material_expired",
        "request_expired", "request_superseded",
    ))
    safe = {key: count for key, count in counters.items() if 1 <= count <= 5}
    if safe:
        print(json.dumps({"gpt_fallback_obsolete": safe}), flush=True)


def _preparation_details(value: object) -> list[dict[str, Any]]:
    token = re.compile(r"[A-Za-z0-9_.:-]{1,40}")
    result: list[dict[str, Any]] = []
    for item in value[:5] if isinstance(value, list) else []:
        if not isinstance(item, dict) or type(item.get("raw_item_id")) is not int:
            continue
        reviews = []
        for review in item.get("reviews", [])[:3] if isinstance(item.get("reviews"), list) else []:
            if not isinstance(review, dict):
                continue
            entry: dict[str, Any] = {key: review[key] for key in ("role", "verdict")
                                     if isinstance(review.get(key), str)
                                     and token.fullmatch(review[key])}
            if type(review.get("score")) is int:
                entry["score"] = review["score"]
            flags = review.get("flags")
            entry["flags"] = [f for f in (flags if isinstance(flags, list) else [])[:5]
                              if isinstance(f, str) and token.fullmatch(f)]
            rubric = review.get("rubric")
            if isinstance(rubric, dict):
                entry["rubric"] = {k: v for k, v in rubric.items()
                                   if isinstance(k, str) and token.fullmatch(k) and type(v) is int}
            reviews.append(entry)
        error = item.get("error_type")
        detail = item.get("error_detail")
        entry_out: dict[str, Any] = {
            "raw_item_id": item["raw_item_id"],
            "error_type": error if isinstance(error, str) and token.fullmatch(error)
            else "unknown", "reviews": reviews}
        if isinstance(detail, str) and re.fullmatch(r"[A-Za-z0-9_.:() -]{1,100}", detail):
            entry_out["error_detail"] = detail
        result.append(entry_out)
    return result


def intake_summary(
    saved: dict[str, Any], feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Only statuses and counters of the last intake pass; never text, ids or exception values."""
    result = saved.get("last_result")
    if not isinstance(result, dict):
        return {}
    summary: dict[str, Any] = {}
    for key in ("status",):
        if isinstance(result.get(key), str) and len(result[key]) <= 40:
            summary[key] = result[key]
    if type(result.get("owner_review_exit_code")) is int:
        summary["owner_review_exit_code"] = result["owner_review_exit_code"]
    intake = _counters(result.get("intake"), ("received", "inspected", "telegram_sends"))
    if intake:
        summary["intake"] = intake
    notices = result.get("intake", {}).get("reader_notices", {})
    stages = notices.get("response_seconds", {})
    timing_samples = {stage: stages.get(stage, []) for stage in ("menu", "prompt", "accepted")}
    timing_samples["clarification"] = (feedback or {}).get("response_seconds", [])
    timing_samples["final"] = [
        sample for run in result.get("reader_resolution_passes", [])
        for sample in run.get("response_seconds", [])
    ]
    reader_timings = {}
    for stage, samples in timing_samples.items():
        if isinstance(samples, list):
            seconds = [n for n in samples if type(n) in (int, float) and 0 <= n < 1e9]
            if seconds:
                reader_timings[stage] = {
                    "count": len(seconds), "min": min(seconds), "max": max(seconds),
                }
    if reader_timings:
        summary["reader_response_seconds"] = reader_timings
    owner = result.get("owner_review")
    if isinstance(owner, dict):
        block: dict[str, Any] = {}
        for key in ("status", "error_type", "phase"):
            if isinstance(owner.get(key), str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", owner[key]):
                block[key] = owner[key]
        for key, names in (("preparation", ("prepared", "blocked")),
                           ("actions", ("revised", "rejected", "published", "blocked",
                                        "deferred")),
                           ("queue", ("queued", "uncertain"))):
            counters: dict[str, Any] = dict(_counters(owner.get(key), names))
            if key == "preparation" and isinstance(owner.get(key), dict):
                details = _preparation_details(owner[key].get("details"))
                if details:
                    counters = {**counters, "details": details}
            if counters:
                block[key] = counters
        summary["owner_review"] = block
    return summary


def preview_archive(drive: SnapshotStore, manifest_id: str, work: Path, output: Path) -> int:
    manifest = json.loads(drive.download(identifier(manifest_id)))
    manifest["drive_file_id"] = (manifest.get("drive_file_id")
                                 or manifest["drive_database_file_id"])
    work.mkdir(parents=True, exist_ok=False)
    database = restored(drive, manifest, work)
    export_public(database, output, manifest["created_at"], None)
    return 0


def export_public(database: Path, output: Path, observed_at: str, run_dir: Path | None) -> None:
    command = [sys.executable, "-m", "relocant_media.ops.newsroom_export",
               "--database", str(database), "--output", str(output),
               "--observed-at", observed_at]
    if run_dir is not None:
        command.extend(["--run-directory", str(run_dir)])
    # The projection is disposable. An expensive read-only export must not hold the
    # canonical writer busy after its verified recovery checkpoint is already saved.
    # subprocess.run kills and reaps the exporter before raising TimeoutExpired.
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=PUBLIC_EXPORT_TIMEOUT_SECONDS)


# Each editorial assesses a bounded batch, so a shorter interval is the throughput lever.
EDITORIAL_INTERVAL = timedelta(minutes=30)


def editorial_due(state: dict[str, Any], now: datetime) -> bool:
    """Editorial is due unless one was checkpointed less than 30 minutes ago."""
    last = state.get("last_editorial_at")
    return not isinstance(last, str) or now - datetime.fromisoformat(last) >= EDITORIAL_INTERVAL


def private_quota_observation(directory: Path) -> dict[str, Any] | None:
    path = directory / "ai-calls.jsonl"
    if not path.is_file():
        return None
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines()[-200:]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("error_phase") != "http_error_429":
            continue
        quota = safe_quota(record.get("quota"))
        if quota:
            entries.append(quota)
    return ({"verified_at": datetime.now(UTC).isoformat(), "reports": entries[-5:]}
            if entries else None)


def _busy_state(state: dict[str, Any], environment: Mapping[str, str]) -> dict[str, Any]:
    return {**state, "status": "busy", "attempt": uuid.uuid4().hex,
            "run_id": environment.get("GITHUB_RUN_ID"),
            "run_attempt": environment.get("GITHUB_RUN_ATTEMPT"),
            "started_at": datetime.now(UTC).isoformat()}


def _commit_republication(
    store: CanonicalStore, drive: SnapshotStore, state: dict[str, Any], sha: str,
    updates: dict[str, Any], environment: Mapping[str, str], work: Path,
) -> tuple[dict[str, Any], str]:
    busy = _busy_state(state, environment)
    attempt = busy["attempt"]
    ready = {"intake_state": {}, "last_exit_code": 0, **busy, **updates,
             "status": "ready", "generation": attempt}
    # Both pointer transitions reuse the verified immutable SQLite snapshot.
    # Prepare recovery before claiming so a checkpoint upload failure cannot leave it busy.
    if isinstance(drive, SSHStateClient):
        checkpoint_id = drive.checkpoint(
            ready, _state_digest(busy), work / f"republication-{attempt}.json",
        )
        print(json.dumps({"recovery_checkpoint_sha256": checkpoint_id}),
              file=sys.stderr, flush=True)
    busy_sha = store.write(busy, sha)
    return ready, store.write(ready, busy_sha)


def execute(store: CanonicalStore, drive: SnapshotStore, *, work: Path, output: Path,
            folder: str, mode: str, environment: dict[str, str],
            map_output: Path | None = None) -> int:
    if mode not in MODES:
        raise ValueError("unsupported runtime mode")
    drain = (_drain_options(environment, now=datetime.now(UTC),
                            close_existing=mode == "drain-close")
             if mode in {"drain", "drain-close"} else None)
    if drain is not None and (environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                              or environment.get("GITHUB_ACTIONS") != "true"):
        raise ValueError("Drain доступен только ручному запуску Actions")
    incoming = environment.get("CLOUD_DISCOVERY_BATCH", "")
    batch = parse_batch(incoming) if mode == "discovery" else None
    if incoming and mode != "discovery":
        raise ValueError("discovery input requires discovery mode")
    gpt_input = environment.get("GPT_FALLBACK_BLOB_SHA256", "")
    gpt_cycle = environment.get("GPT_FALLBACK_CYCLE_ID", "")
    if gpt_cycle and (mode != "fallback-preview" or not re.fullmatch(r"[1-9][0-9]{0,18}", gpt_cycle)
                      or int(gpt_cycle) >= 2**63):
        raise ValueError("GPT cycle input requires an exact fallback preview cycle")
    if gpt_input and mode not in {"fallback", "map-republish"}:
        raise ValueError("GPT result input requires fallback or map-republish mode")
    map_input = environment.get("MAP_BACKFILL_TARGETS", "")
    if map_input and mode != "map-backfill":
        raise ValueError("map button targets require map-backfill mode")
    targets = parse_map_button_targets(map_input) if mode == "map-backfill" else {}
    repost_input = environment.get("MAP_REPUBLICATION_REQUEST", "")
    map_repost_modes = {"map-republish", "map-republish-review"}
    if repost_input and mode not in map_repost_modes:
        raise ValueError("map republication request requires a republication mode")
    repost = (MapRepublicationRequest.model_validate_json(repost_input)
              if mode in map_repost_modes else None)
    previous_digest = repost.previous_review_sha256 if repost is not None else None
    if (mode in {"fallback-preview", "fallback", "map-republish-review"} or gpt_input
            or previous_digest):
        if (environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or environment.get("EDITORIAL_STORAGE_BACKEND") != "ssh"
                or not isinstance(drive, SSHStateClient)):
            raise ValueError("GPT exchange requires a manual SSH-backed Actions run")
        if gpt_input or mode == "fallback":
            _gpt_digest(gpt_input)
    _runtime_phase("state_read_started")
    state, sha = store.read()
    validate_ready(state)
    ingress_config(state.get("intake_state", {}))
    _runtime_phase("state_read_verified")
    if mode == "auto":
        mode = "editorial" if editorial_due(state, datetime.now(UTC)) else "intake"
    slot = (due_digest_slot(datetime.now(UTC)) if mode in {"digest", "editorial", "intake"}
            and environment.get("GITHUB_ACTIONS") == "true"
            and environment.get("EDITORIAL_SEND_ENABLED") == "true"
            and environment.get("EDITORIAL_CHANNEL_MODE") == "public"
            and isinstance(drive, SSHStateClient) else None)
    if mode == "digest" and slot is None:
        return 0
    work.mkdir(parents=True, exist_ok=False, mode=0o700)
    _runtime_phase("restore_started")
    database = restored(drive, state["snapshot"], work)
    _runtime_phase("restore_verified")
    if mode == "digest-preview":
        if environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
            raise ValueError("digest preview requires a manual request")
        when = datetime.fromisoformat(environment["DIGEST_PREVIEW_AT"])
        result = asyncio.run(preview_digest(
            database, when, environment.get("RELOCANT_MAP_TELEGRAM_APP", ""),
        ))
        print(json.dumps({"digest_preview": result}, ensure_ascii=False), flush=True)
        return 0
    if mode == "preview":
        export_public(database, output, state["snapshot"]["created_at"], None)
        if map_output is not None:
            export_map_snapshot(
                database, map_output, state["snapshot"]["created_at"],
                street_index=BELGRADE_STREETS if BELGRADE_STREETS.is_file() else None,
                area_index=BELGRADE_AREAS if BELGRADE_AREAS.is_file() else None,
            )
            try:
                export_events_snapshot(database, map_output.parent / "events.json",
                                       state["snapshot"]["created_at"])
            except Exception as error:
                print(json.dumps({"events_projection_error_type": type(error).__name__}),
                      file=sys.stderr)
        return 0
    if (environment.get("ACTIONS_CUTOVER_CONFIRMED") != "true"
            or environment.get("GITHUB_REPOSITORY") != REPO
            or environment.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("main-only confirmed cutover required")
    if mode == "map-backfill":
        if (environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or environment.get("EDITORIAL_SEND_ENABLED") != "true"):
            raise ValueError("manual editorial delivery must be enabled for map button updates")
        asyncio.run(apply_map_buttons(
            database, targets, environment.get("RELOCANT_MAP_TELEGRAM_APP", ""),
            environment.get("TELEGRAM_BOT_TOKEN", ""),
        ))
        return 0
    if repost is not None:
        if (environment.get("GITHUB_ACTIONS") != "true"
                or environment.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
            raise ValueError("map republication requires an explicit hosted editorial request")
        previous = (resolve_map_republication(repost, state, _gpt_blob(drive, previous_digest))
                    if previous_digest else None)
        if mode == "map-republish-review":
            request = map_review_request(database, repost, previous)
            digest = _upload_gpt_request(drive, request, work / "map-review-request.json")
            print(json.dumps({"map_republication_review_sha256": digest}), flush=True)
            return 0
        if environment.get("EDITORIAL_SEND_ENABLED") != "true":
            raise ValueError("map republication requires enabled editorial delivery")
        gpt = None
        if gpt_input:
            reviews = MapGPTReviews.model_validate(_gpt_blob(drive, gpt_input))
            gpt = reviews, _gpt_blob(drive, reviews.request_sha256)

        def commit(current: dict[str, Any], digest: str,
                   updates: dict[str, Any]) -> tuple[dict[str, Any], str]:
            return _commit_republication(store, drive, current, digest, updates, environment, work)

        apply_map_republication(database, repost, state, sha, commit, environment, gpt, previous)
        return 0
    if mode == "fallback-preview":
        return _export_gpt_request(drive, database, work, int(gpt_cycle) if gpt_cycle else None)
    digest_attempt: dict[str, Any] = {}
    if slot is not None:
        def commit_digest(current: dict[str, Any], digest: str,
                          updates: dict[str, Any]) -> tuple[dict[str, Any], str]:
            return _commit_republication(store, drive, current, digest, updates, environment, work)

        result = asyncio.run(run_digest(database, slot, state, sha, commit_digest, environment))
        digest_log = {"digest": slot.key, "status": result["state"],
                      "message_id": result.get("message_id")}
        if weather_sources := result.get("weather_sources"):
            digest_log["weather_sources"] = weather_sources
        print(json.dumps(digest_log), flush=True)
        if result["state"] in {"sent", "uncertain"}:
            state, sha = store.read()
            validate_ready(state)
        if result["state"] in DIGEST_SKIP_REASONS.get(slot.kind, ()):
            key = DIGEST_ATTEMPT_KEYS[slot.kind]
            attempt_result = {"slot": slot.key, "reason": result["state"]}
            if state.get(key) != attempt_result:
                digest_attempt[key] = attempt_result
                if mode == "digest":
                    commit_digest(state, sha, digest_attempt)
    if mode == "digest":
        return 0
    fallback_file = work / "gpt-fallback-input.json"
    fallback_submission: Path | None = None
    without_gpt_delivery = False
    if mode == "fallback":
        without_gpt_delivery = _prepare_gpt_input(drive, gpt_input, fallback_file)
        result = json.loads(fallback_file.read_text(encoding="utf-8"))["result"]
        submission = (
            verified_submission(environment, gpt_input)
            if (result.get("packages") or result.get("rejected"))
            and hashlib.sha256(canonical_json(result).encode()).hexdigest() == gpt_input
            else None
        )
        if _gpt_result_expired(database, fallback_file, datetime.now(UTC), submission):
            # До захвата блокировки: состояние не меняется, повтор ничего не даст.
            print(json.dumps({"gpt_fallback": "result_expired",
                              "reason": "material_closed"}), flush=True)
            return 0
        if submission is not None:
            fallback_submission = work / "gpt-submission.json"
            fallback_submission.write_text(canonical_json(submission.audit()), encoding="utf-8")
    # Readback/restore and all validation precede the claim; all side effects follow it.
    identifier(folder)
    require_snapshot_capacity(database)
    busy = _busy_state(state, environment)
    attempt = busy["attempt"]
    _runtime_phase("state_claim_started")
    busy_sha = store.write(busy, sha)
    _runtime_phase("state_claimed")
    if mode != "drain-close":
        _runtime_phase("lifecycle_before_started")
        changed = _sweep_material_lifecycle(database)
        _runtime_phase("lifecycle_before_changed" if changed else "lifecycle_before_unchanged")
    directory = database.parent
    intake_state = directory / "intake-batch-state.json"
    intake_state.write_text(json.dumps(state.get("intake_state", {})), encoding="utf-8")
    env = {**environment, "DATABASE_URL": f"sqlite:///{database.resolve().as_posix()}",
           "RELOCANT_RUNTIME_PLATFORM": "github-actions", "TELEGRAM_MTPROTO_ENABLED": "false",
           "EDITORIAL_ACTIONS_CHECKPOINT": "true"}
    for name in list(env):
        if name.startswith("EDITORIAL_SSH_") or name in {
            "EDITORIAL_STORAGE_BACKEND", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
            "GH_STATE_TOKEN", "GH_STATE_REPOSITORY", "DRIVE_CREDENTIALS_JSON",
            "DRIVE_STATE_FOLDER_ID", "DRIVE_PREVIEW_MANIFEST_ID", "GOOGLE_APPLICATION_CREDENTIALS",
            "CLOUD_DISCOVERY_BATCH",
            "GH_TOKEN",
            "GPT_FALLBACK_BLOB_SHA256",
            "GPT_FALLBACK_CYCLE_ID",
        }:
            env.pop(name, None)
    # The worker creates its own output directory and refuses one that already exists.
    run_dir = work / "cycle"
    discovery_ids: list[int] = []
    created = 0
    if batch is not None:
        engine = create_engine(env["DATABASE_URL"])
        try:
            with Session(engine) as session:
                discovery_ids, created = ingest_batch(session, batch)
                session.commit()
        finally:
            engine.dispose()
    if mode == "fallback":
        if without_gpt_delivery:
            env.pop("TELEGRAM_BOT_TOKEN", None)
            env["EDITORIAL_SEND_ENABLED"] = "false"
        for name in list(env):
            if name in {"GEMINI_API_KEY", "GEMINI_MODEL", "TYPESAFE_API_KEY"} or name.startswith(
                ("EDITORIAL_PRIVATE_", "EDITORIAL_OWNER_", "GEMINI_MODEL_")
            ):
                env.pop(name)
        command = [sys.executable, "-m", "relocant_media.ops.gpt_fallback_worker",
                   "--input", str(fallback_file), "--output", str(run_dir)]
        if fallback_submission is not None:
            command.extend(["--submission", str(fallback_submission)])
    elif mode == "intake":
        env["EDITORIAL_INTAKE_MODE"] = "batch"
        command = [sys.executable, "-m", "relocant_media.ops.intake_batch"]
    elif drain is not None:
        # Ни сбор, ни intake, ни предложка не входят в фиксированную старую очередь.
        env["EDITORIAL_PRIVATE_INTAKE_ENABLED"] = "false"
        env["EDITORIAL_PRIVATE_FEEDBACK_ENABLED"] = "false"
        env["EDITORIAL_OWNER_REVIEW_ENABLED"] = "false"
        env.pop("GITHUB_OUTPUT", None)
        command = [sys.executable, "-m", "relocant_media.ops.backlog_drain",
                   "--before", drain.before.isoformat(),
                   "--max-attempts", str(drain.max_attempts),
                   "--max-seconds", str(drain.max_seconds),
                   "--runs-left", str(drain.runs_left), "--output", str(run_dir)]
        if drain.close_existing:
            command.append("--close-existing")
            env["EDITORIAL_SEND_ENABLED"] = "false"
            for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "TELEGRAM_BOT_TOKEN",
                        "TYPESAFE_API_KEY"):
                env.pop(key, None)
        elif env.get("EDITORIAL_SEND_ENABLED") == "true":
            command.append("--send-enabled")
    else:
        # The five-minute batch is the only update consumer; no second getUpdates poller.
        env["EDITORIAL_INTAKE_MODE"] = "batch"
        if isinstance(drive, SSHStateClient) and mode == "editorial":
            env["EDITORIAL_GPT_FALLBACK_ENABLED"] = "true"
        command = [sys.executable, "-m", "relocant_media.ops.server_runtime",
                   "--output", str(run_dir)]
        if mode == "discovery":
            command.append("--submissions-only")
            env["EDITORIAL_PRIVATE_FEEDBACK_ENABLED"] = "false"
    # Replayed intake observes the existing receipt; it never re-triggers a send.
    if mode == "discovery" and not created:
        _runtime_phase("worker_skipped")
        code = 0
    else:
        _runtime_phase("worker_started")
        if mode != "intake":
            env.pop("EDITORIAL_INGRESS_TOKEN", None)
        code = run_child(command, env)
        _runtime_phase("worker_completed")
    drain_control = (False, 0)
    if drain is not None:
        try:
            drain_control = _drain_control(run_dir, drain, code)
        except (OSError, ValueError, TypeError):
            # Невалидный отчёт не разрешает chain; квитанции всё равно сохраняются.
            code = code or 1
    report_failure(run_dir, code)
    if mode == "fallback":
        _report_fallback_result(run_dir)
    # A signal/forced termination has uncertain effects: do not unlock even if bytes survived.
    if code < 0:
        raise RuntimeError("runtime terminated; reconcile busy generation")
    if mode != "drain-close":
        _runtime_phase("lifecycle_after_started")
        changed = _sweep_material_lifecycle(database)
        _runtime_phase("lifecycle_after_changed" if changed else "lifecycle_after_unchanged")
    _runtime_phase("snapshot_started")
    manifest = create_snapshot(database, work / "saved")
    _runtime_phase("snapshot_local_verified")
    if manifest["sha256"] == state["snapshot"]["sha256"]:
        manifest["drive_file_id"] = state["snapshot"]["drive_file_id"]
        _runtime_phase("snapshot_storage_reused")
    else:
        manifest["drive_file_id"] = drive.upload(work / "saved" / "editorial.sqlite3", folder)
        _runtime_phase("snapshot_storage_written")
    _runtime_phase("snapshot_verified")
    saved_intake = json.loads(intake_state.read_text())
    # Preserve retry and ingress state. The verified snapshot satisfies any local backup request.
    budget = {key: saved_intake[key] for key in (
        "signature", "attempts", "ingress", "serverless_sequence")
              if key in saved_intake}
    ingress_config(budget)
    canonical_intake = state.get("intake_state", {})
    if budget.get("ingress") != canonical_intake.get("ingress"):
        raise ValueError("worker cannot change canonical ingress configuration")
    if (sequence_cursor(budget.get("serverless_sequence", 0))
            < sequence_cursor(canonical_intake.get("serverless_sequence", 0))):
        raise ValueError("ingress cursor cannot move backwards")
    has_cycle_report = mode in {"editorial", "fallback"} or (mode == "discovery" and created)
    observed_run = run_dir if has_cycle_report else next(
        iter(sorted((directory / "runs").glob("*/server-report.json"), reverse=True)), None)
    if observed_run is not None and observed_run.is_file():
        observed_run = observed_run.parent
    if observed_run is not None and not observed_run.is_dir():
        observed_run = None  # the worker failed before creating its output
    feedback_stats = None
    intake_result = saved_intake.get("last_result", {})
    if (
        observed_run is not None and mode == "intake"
        and intake_result.get("pipeline_started")
    ):
        pipeline_code = intake_result.get("pipeline_exit_code")
        if type(pipeline_code) is int:
            report_failure(observed_run, pipeline_code)
        try:
            report = json.loads((observed_run / "server-report.json").read_text(encoding="utf-8"))
            feedback_stats = report.get("private_feedback")
        except (OSError, ValueError):
            pass
    private_intake_report = intake_summary(saved_intake, feedback_stats)
    if timings := private_intake_report.get("reader_response_seconds"):
        print(json.dumps({"reader_response_seconds": timings}), flush=True)
    last_cycle = state.get("last_cycle")
    ready = {**busy, "status": "ready", "generation": attempt, "snapshot": manifest,
             "intake_state": budget, "finished_at": datetime.now(UTC).isoformat(),
             "last_exit_code": code, "last_cycle": last_cycle,
             "last_intake": private_intake_report or state.get("last_intake") or {},
             "last_editorial_at": (
                 datetime.now(UTC).isoformat() if mode == "editorial" and code == 0
                 else state.get("last_editorial_at"))}
    ready.update(digest_attempt)
    if batch is not None:
        engine = create_engine(env["DATABASE_URL"])
        try:
            with Session(engine) as session:
                ready["last_discovery"] = {
                    "created": created, "runtime_exit_code": code,
                    "run_id": environment.get("GITHUB_RUN_ID"),
                    "batch_sha256": hashlib.sha256(
                        batch.model_dump_json().encode()).hexdigest(),
                    "candidates": batch_receipt(session, discovery_ids),
                }
        finally:
            engine.dispose()
    quota_observation = private_quota_observation(run_dir)
    if quota_observation is not None:
        ready["last_quota_observation"] = {
            **quota_observation, "run_id": environment.get("GITHUB_RUN_ID")}
    # SSH retains a private, read-back-verified recovery intent before final CAS.
    # Its digest is safe to log; the envelope and SQLite remain private blobs.
    if isinstance(drive, SSHStateClient):
        _runtime_phase("recovery_checkpoint_started")
        checkpoint_id = drive.checkpoint(ready, busy_sha, work / "checkpoint.json")
        _runtime_phase("recovery_checkpoint_verified")
        print(json.dumps({"recovery_checkpoint_sha256": checkpoint_id}),
              file=sys.stderr, flush=True)
    # Preserve the prepared fix from fix/pages-failed-runtime-observation: an
    # uncommitted queue must never appear in the directory uploaded by Actions,
    # even if a hard termination bypasses main's exception cleanup.
    provisional = work / "public-projection.json"
    projection_failed = False
    last_cycle = state.get("last_cycle")
    try:
        _runtime_phase("projection_started")
        export_public(database, provisional, manifest["created_at"], observed_run)
        last_cycle = state.get("last_cycle")
        if provisional.is_file():
            projection = json.loads(provisional.read_text())
            city = projection["cities"][0]
            if "last_cycle" not in city and isinstance(last_cycle, dict):
                city["last_cycle"] = last_cycle
            if brief := city.get("daily_brief"):
                editions = public_digest_days(ready, datetime.fromisoformat(manifest["created_at"]))
                for day in brief["days"]:
                    day.update(editions.get(day["local_date"], {}))
            projection = merge(projection, projection)
            last_cycle = projection["cities"][0].get("last_cycle")
            provisional.write_text(json.dumps(projection, ensure_ascii=False), encoding="utf-8")
        _runtime_phase("projection_completed")
    except Exception as error:
        # Projection is disposable; verified receipts and retry budget are not.
        provisional.unlink(missing_ok=True)
        projection_failed = True
        _runtime_phase("projection_failed")
        print(json.dumps({"projection_error_type": type(error).__name__}), file=sys.stderr)
    ready["last_cycle"] = last_cycle
    _runtime_phase("state_commit_started")
    store.write(ready, busy_sha)
    _runtime_phase("state_committed")
    if mode in {"editorial", "intake"} and isinstance(drive, SSHStateClient):
        if code == 0 and not projection_failed:
            try:
                (work / "observation-receipt.json").write_text(json.dumps({
                    "run_id": ready["run_id"],
                    "run_attempt": ready["run_attempt"],
                    "started_at": ready["started_at"],
                    "snapshot_sha256": manifest["sha256"],
                }), encoding="utf-8")
            except OSError as error:
                print(json.dumps({"observation_receipt_error_type": type(error).__name__}),
                      file=sys.stderr)
        _write_digest_watch(ready, environment, now=datetime.now(UTC))
    if (mode in {"editorial", "drain"} and code == 0
            and environment.get("GITHUB_ACTIONS") == "true"
            and environment.get("EDITORIAL_STORAGE_BACKEND") == "ssh"
            and isinstance(drive, SSHStateClient)):
        try:
            _export_committed_fallback(drive, work / "saved" / "editorial.sqlite3", work,
                                       ready=ready, mode=mode, environment=environment)
        except Exception as error:
            # Канонический commit уже завершён. Optional export не меняет его исход.
            print(json.dumps({"gpt_fallback_export_error_type": type(error).__name__}),
                  file=sys.stderr, flush=True)
    if drain is not None:
        _write_drain_control(environment, drain_control)
    refreshed_request = run_dir / "gpt-refreshed-request.json"
    if mode == "fallback" and code == 0 and refreshed_request.is_file():
        request = json.loads(refreshed_request.read_text(encoding="utf-8"))
        digest = _upload_gpt_request(drive, request, work / "gpt-request.json")
        print(json.dumps({"gpt_fallback_request_sha256": digest,
                          "candidate_count": len(request["candidates"])}), flush=True)
    # Purge ingress copies only after snapshot verification and successful canonical CAS.
    # A lost ACK leaves harmless private copies; the next ready checkpoint retries it.
    source = ingress_config(budget)
    through = sequence_cursor(budget.get("serverless_sequence", 0))
    if source is not None and source["phase"] == "active" and through > 0:
        try:
            acknowledge_inbox(source["url"], environment.get("EDITORIAL_INGRESS_TOKEN", ""),
                              through, manifest["sha256"],
                              open_drafts=open_draft_routes(work / "saved" / "editorial.sqlite3"))
        except Exception as error:
            print(json.dumps({"ingress_ack_error_type": type(error).__name__}), file=sys.stderr)
    # Publication of this artifact happens only after the canonical checkpoint.
    if provisional.is_file():
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        shutil.copyfile(provisional, temporary)
        temporary.replace(output)
    if map_output is not None:
        try:
            export_map_snapshot(
                work / "saved" / "editorial.sqlite3", map_output, manifest["created_at"],
                street_index=BELGRADE_STREETS if BELGRADE_STREETS.is_file() else None,
                area_index=BELGRADE_AREAS if BELGRADE_AREAS.is_file() else None,
            )
        except Exception as error:
            print(json.dumps({"map_projection_error_type": type(error).__name__}),
                  file=sys.stderr)
        try:
            export_events_snapshot(work / "saved" / "editorial.sqlite3",
                                   map_output.parent / "events.json", manifest["created_at"])
        except Exception as error:
            print(json.dumps({"events_projection_error_type": type(error).__name__}),
                  file=sys.stderr)
    return code or int(projection_failed)


def bootstrap_record(manifest: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    """Called only by explicit offline cutover tooling, never scheduled execution."""
    observed = datetime.fromisoformat(manifest["created_at"])
    if observed.tzinfo is None or not timedelta(0) <= now - observed <= timedelta(hours=1):
        raise ValueError("fresh final snapshot after fencing all former writers required")
    state = {"version": 1, "owner": "github-actions", "status": "ready",
             "generation": uuid.uuid4().hex, "snapshot": manifest,
             "cutover": {"server_fenced": True, "at": now.isoformat()}, "intake_state": {}}
    validate_ready(state)
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=sorted(MODES),
                        default="preview")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--map-output", type=Path)
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            if args.mode == "preview" and os.environ.get("DRIVE_PREVIEW_MANIFEST_ID"):
                if os.environ.get("EDITORIAL_STORAGE_BACKEND", "drive") != "drive":
                    raise ValueError("Drive archive preview requires the drive backend")
                code = preview_archive(Drive(client), os.environ["DRIVE_PREVIEW_MANIFEST_ID"],
                                       args.work, args.output)
            else:
                store, blobs, folder = build_storage(client, os.environ)
                code = execute(store, blobs,
                               work=args.work, output=args.output, mode=args.mode,
                               folder=folder, environment=dict(os.environ),
                               map_output=args.map_output)
    except Exception as error:
        args.output.unlink(missing_ok=True)
        if args.map_output is not None:
            args.map_output.unlink(missing_ok=True)
            (args.map_output.parent / "events.json").unlink(missing_ok=True)
        # HTTP exception messages can contain URLs/credentials/private response bodies.
        blocked = {"status": "blocked", "error_type": type(error).__name__}
        for field in ("code", "reason"):
            value = getattr(error, field, None)
            if isinstance(value, str) and re.fullmatch(r"[a-z_]{1,32}", value):
                blocked[f"storage_{field}"] = value
        print(json.dumps(blocked))
        raise SystemExit(1) from None
    print(json.dumps({"status": "completed" if code == 0 else "runtime_failed"}))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
