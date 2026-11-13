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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import google.auth
import httpx
from google.auth.transport.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ai.package import canonical_json
from relocant_media.ai.quota_diagnostics import safe_quota
from relocant_media.ops.belgrade_digests import due_slot as due_digest_slot
from relocant_media.ops.belgrade_digests import run as run_digest
from relocant_media.ops.events_export import export_events_snapshot
from relocant_media.ops.map_backfill import apply as apply_map_buttons
from relocant_media.ops.map_backfill import parse_targets as parse_map_button_targets
from relocant_media.ops.map_export import export_map_snapshot
from relocant_media.ops.map_republication import GPTReviews as MapGPTReviews
from relocant_media.ops.map_republication import Request as MapRepublicationRequest
from relocant_media.ops.map_republication import apply as apply_map_republication
from relocant_media.ops.map_republication import resolve_previous as resolve_map_republication
from relocant_media.ops.map_republication import review_request as map_review_request
from relocant_media.ops.newsroom_merge import merge
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.ops.sqlite_restore import restore
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient, _state_digest
from relocant_media.services.cloud_discovery import batch_receipt, ingest_batch, parse_batch

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
    "map-backfill", "map-republish", "map-republish-review", "digest",
}


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


def _prepare_gpt_input(storage: SnapshotStore, digest: str, path: Path) -> None:
    result = _gpt_blob(storage, digest)
    packages = result.get("packages")
    if (set(result) != {"version", "request_sha256", "packages"}
            or type(result.get("version")) is not int or result["version"] != 1
            or not isinstance(packages, list) or not 1 <= len(packages) <= 5):
        raise ValueError("invalid GPT result envelope")
    request_sha = _gpt_digest(result["request_sha256"])
    request = _gpt_blob(storage, request_sha)
    if hashlib.sha256(canonical_json(request).encode()).hexdigest() != request_sha:
        raise ValueError("GPT request blob is not canonical")
    path.write_text(canonical_json({"request": request, "result": result}), encoding="utf-8")


def _upload_gpt_request(storage: SnapshotStore, request: dict[str, Any], path: Path) -> str:
    content = canonical_json(request).encode()
    if len(content) > GPT_EXCHANGE_MAX_BYTES:
        raise ValueError("GPT request exceeds private exchange bound")
    path.write_bytes(content)
    digest = storage.upload(path, "gpt-fallback-request")
    if digest != hashlib.sha256(content).hexdigest():
        raise ValueError("GPT request storage receipt differs")
    return digest


def _export_gpt_request(storage: SnapshotStore, database: Path, work: Path) -> int:
    from relocant_media.services.gpt_fallback import build_request

    engine = create_engine(f"sqlite:///{database.resolve().as_posix()}")
    try:
        with Session(engine) as session:
            request = build_request(session, now=datetime.now(UTC))
    finally:
        engine.dispose()
    if request is None:
        print(json.dumps({"gpt_fallback": "no_pending_candidates"}))
        return 0
    digest = _upload_gpt_request(storage, request, work / "gpt-request.json")
    print(json.dumps({"gpt_fallback_request_sha256": digest,
                      "candidate_count": len(request["candidates"])}))
    return 0


def _runtime_phase(phase: str) -> None:
    """Только фиксированные имена стадий; никаких исходных текстов и реквизитов."""
    print(json.dumps({"runtime_phase": phase}), file=sys.stderr, flush=True)


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


def print_failure_coordinates(report: dict[str, Any]) -> None:
    fields = {key: value for key in ("status", "error_type", "error_site", "backup_error_type")
              if isinstance(value := report.get(key), str)
              and re.fullmatch(r"[A-Za-z0-9_.:]{1,80}", value)}
    print(json.dumps({"runtime_failure": fields}), file=sys.stderr)


def report_failure(run_dir: Path, code: int) -> None:
    """Child output stays hidden; print only whitelisted failure coordinates to stderr."""
    if code == 0:
        return
    try:
        report = json.loads((run_dir / "server-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    print_failure_coordinates(report)


def _counters(value: object, keys: tuple[str, ...]) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in keys if type(value.get(key)) is int}


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


def intake_summary(saved: dict[str, Any]) -> dict[str, Any]:
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
    incoming = environment.get("CLOUD_DISCOVERY_BATCH", "")
    batch = parse_batch(incoming) if mode == "discovery" else None
    if incoming and mode != "discovery":
        raise ValueError("discovery input requires discovery mode")
    gpt_input = environment.get("GPT_FALLBACK_BLOB_SHA256", "")
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
    state, sha = store.read()
    validate_ready(state)
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
    database = restored(drive, state["snapshot"], work)
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
        return _export_gpt_request(drive, database, work)
    if slot is not None:
        def commit_digest(current: dict[str, Any], digest: str,
                          updates: dict[str, Any]) -> tuple[dict[str, Any], str]:
            return _commit_republication(store, drive, current, digest, updates, environment, work)

        result = asyncio.run(run_digest(database, slot, state, sha, commit_digest, environment))
        print(json.dumps({"digest": slot.key, "status": result["state"],
                          "message_id": result.get("message_id")}), flush=True)
        if result["state"] in {"sent", "uncertain"}:
            state, sha = store.read()
            validate_ready(state)
    if mode == "digest":
        return 0
    fallback_file = work / "gpt-fallback-input.json"
    if mode == "fallback":
        _prepare_gpt_input(drive, gpt_input, fallback_file)
    # Readback/restore and all validation precede the claim; all side effects follow it.
    identifier(folder)
    require_snapshot_capacity(database)
    busy = _busy_state(state, environment)
    attempt = busy["attempt"]
    busy_sha = store.write(busy, sha)
    directory = database.parent
    intake_state = directory / "intake-batch-state.json"
    intake_state.write_text(json.dumps(state.get("intake_state", {})), encoding="utf-8")
    env = {**environment, "DATABASE_URL": f"sqlite:///{database.resolve().as_posix()}",
           "RELOCANT_RUNTIME_PLATFORM": "github-actions", "TELEGRAM_MTPROTO_ENABLED": "false"}
    for name in list(env):
        if name.startswith("EDITORIAL_SSH_") or name in {
            "EDITORIAL_STORAGE_BACKEND", "SSH_AUTH_SOCK", "SSH_AGENT_PID",
            "GH_STATE_TOKEN", "GH_STATE_REPOSITORY", "DRIVE_CREDENTIALS_JSON",
            "DRIVE_STATE_FOLDER_ID", "DRIVE_PREVIEW_MANIFEST_ID", "GOOGLE_APPLICATION_CREDENTIALS",
            "CLOUD_DISCOVERY_BATCH",
            "GPT_FALLBACK_BLOB_SHA256",
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
        for name in list(env):
            if name in {"GEMINI_API_KEY", "GEMINI_MODEL", "TYPESAFE_API_KEY"} or name.startswith(
                ("EDITORIAL_PRIVATE_", "EDITORIAL_OWNER_")
            ):
                env.pop(name)
        command = [sys.executable, "-m", "relocant_media.ops.gpt_fallback_worker",
                   "--input", str(fallback_file), "--output", str(run_dir)]
    elif mode == "intake":
        env["EDITORIAL_INTAKE_MODE"] = "batch"
        command = [sys.executable, "-m", "relocant_media.ops.intake_batch"]
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
        code = run_child(command, env)
        _runtime_phase("worker_completed")
    report_failure(run_dir, code)
    # A signal/forced termination has uncertain effects: do not unlock even if bytes survived.
    if code < 0:
        raise RuntimeError("runtime terminated; reconcile busy generation")
    _runtime_phase("snapshot_started")
    manifest = create_snapshot(database, work / "saved")
    manifest["drive_file_id"] = (state["snapshot"]["drive_file_id"]
        if manifest["sha256"] == state["snapshot"]["sha256"]
        else drive.upload(work / "saved" / "editorial.sqlite3", folder))
    _runtime_phase("snapshot_verified")
    saved_intake = json.loads(intake_state.read_text())
    # Retry budget lives outside SQLite; preserve it across ephemeral runners.
    budget = {key: saved_intake[key] for key in ("signature", "attempts", "backup_pending")
              if key in saved_intake}
    has_cycle_report = mode in {"editorial", "fallback"} or (mode == "discovery" and created)
    observed_run = run_dir if has_cycle_report else next(
        iter(sorted((directory / "runs").glob("*/server-report.json"), reverse=True)), None)
    if observed_run is not None and observed_run.is_file():
        observed_run = observed_run.parent
    if observed_run is not None and not observed_run.is_dir():
        observed_run = None  # the worker failed before creating its output
    last_cycle = state.get("last_cycle")
    ready = {**busy, "status": "ready", "generation": attempt, "snapshot": manifest,
             "intake_state": budget, "finished_at": datetime.now(UTC).isoformat(),
             "last_exit_code": code, "last_cycle": last_cycle,
             "last_intake": intake_summary(saved_intake) or state.get("last_intake") or {},
             "last_editorial_at": (
                 datetime.now(UTC).isoformat() if mode == "editorial" and code == 0
                 else state.get("last_editorial_at"))}
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
