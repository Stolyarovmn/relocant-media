"""Hosted compute with immutable SSH/Drive backups and a fail-closed CAS state pointer.

No automatic bootstrap, lock expiry, send retries, schema creation, or fallback DB.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
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

from relocant_media.ops.newsroom_merge import merge
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.ops.sqlite_restore import restore
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient
from relocant_media.services.cloud_discovery import batch_receipt, ingest_batch, parse_batch

REPO = "Stolyarovmn/relocant-media"
STATE_BRANCH = "editorial-state"
STATE_PATH = "state/current.json"
MAX_BYTES = 50 * 1024 * 1024


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
    child = subprocess.Popen(command, env=environment, start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        return child.wait(timeout=4100)
    except BaseException:
        os.killpg(child.pid, signal.SIGKILL)
        child.wait()
        raise


def report_failure(run_dir: Path, code: int) -> None:
    """Child output stays hidden; print only whitelisted failure coordinates to stderr."""
    if code == 0:
        return
    try:
        report = json.loads((run_dir / "server-report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    fields = {key: value for key in ("status", "error_type", "error_site", "backup_error_type")
              if isinstance(value := report.get(key), str)
              and re.fullmatch(r"[A-Za-z0-9_.:]{1,80}", value)}
    print(json.dumps({"runtime_failure": fields}), file=sys.stderr)


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
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# Each editorial assesses a bounded batch, so a shorter interval is the throughput lever.
EDITORIAL_INTERVAL = timedelta(minutes=30)


def editorial_due(state: dict[str, Any], now: datetime) -> bool:
    """Editorial is due unless one was checkpointed less than 30 minutes ago."""
    last = state.get("last_editorial_at")
    return not isinstance(last, str) or now - datetime.fromisoformat(last) >= EDITORIAL_INTERVAL


def execute(store: CanonicalStore, drive: SnapshotStore, *, work: Path, output: Path,
            folder: str, mode: str, environment: dict[str, str]) -> int:
    if mode not in {"preview", "editorial", "intake", "discovery", "auto"}:
        raise ValueError("unsupported runtime mode")
    incoming = environment.get("CLOUD_DISCOVERY_BATCH", "")
    batch = parse_batch(incoming) if mode == "discovery" else None
    if incoming and mode != "discovery":
        raise ValueError("discovery input requires discovery mode")
    state, sha = store.read()
    validate_ready(state)
    if mode == "auto":
        mode = "editorial" if editorial_due(state, datetime.now(UTC)) else "intake"
    work.mkdir(parents=True, exist_ok=False)
    database = restored(drive, state["snapshot"], work)
    if mode == "preview":
        export_public(database, output, state["snapshot"]["created_at"], None)
        return 0
    if (environment.get("ACTIONS_CUTOVER_CONFIRMED") != "true"
            or environment.get("GITHUB_REPOSITORY") != REPO
            or environment.get("GITHUB_REF") != "refs/heads/main"):
        raise ValueError("main-only confirmed cutover required")
    # Readback/restore and all validation precede the claim; all side effects follow it.
    identifier(folder)
    attempt = uuid.uuid4().hex
    busy = {**state, "status": "busy", "attempt": attempt,
            "run_id": environment.get("GITHUB_RUN_ID"),
            "run_attempt": environment.get("GITHUB_RUN_ATTEMPT"),
            "started_at": datetime.now(UTC).isoformat()}
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
    if mode == "intake":
        env["EDITORIAL_INTAKE_MODE"] = "batch"
        command = [sys.executable, "-m", "relocant_media.ops.intake_batch"]
    else:
        # The five-minute batch is the only update consumer; no second getUpdates poller.
        env["EDITORIAL_INTAKE_MODE"] = "batch"
        command = [sys.executable, "-m", "relocant_media.ops.server_runtime",
                   "--output", str(run_dir)]
        if mode == "discovery":
            command.append("--submissions-only")
            env["EDITORIAL_PRIVATE_FEEDBACK_ENABLED"] = "false"
    # Replayed intake observes the existing receipt; it never re-triggers a send.
    code = 0 if mode == "discovery" and not created else run_child(command, env)
    report_failure(run_dir, code)
    # A signal/forced termination has uncertain effects: do not unlock even if bytes survived.
    if code < 0:
        raise RuntimeError("runtime terminated; reconcile busy generation")
    manifest = create_snapshot(database, work / "saved")
    manifest["drive_file_id"] = (state["snapshot"]["drive_file_id"]
        if manifest["sha256"] == state["snapshot"]["sha256"]
        else drive.upload(work / "saved" / "editorial.sqlite3", folder))
    saved_intake = json.loads(intake_state.read_text())
    # Retry budget lives outside SQLite; preserve it across ephemeral runners.
    budget = {key: saved_intake[key] for key in ("signature", "attempts", "backup_pending")
              if key in saved_intake}
    observed_run = run_dir if mode in {"editorial", "discovery"} and (mode != "discovery"
        or created) else next(
        iter(sorted((directory / "runs").glob("*/server-report.json"), reverse=True)), None)
    if observed_run is not None and observed_run.is_file():
        observed_run = observed_run.parent
    if observed_run is not None and not observed_run.is_dir():
        observed_run = None  # the worker failed before creating its output
    # Preserve the prepared fix from fix/pages-failed-runtime-observation: an
    # uncommitted queue must never appear in the directory uploaded by Actions,
    # even if a hard termination bypasses main's exception cleanup.
    provisional = work / "public-projection.json"
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
    ready = {**busy, "status": "ready", "generation": attempt, "snapshot": manifest,
             "intake_state": budget, "finished_at": datetime.now(UTC).isoformat(),
             "last_exit_code": code, "last_cycle": last_cycle,
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
    store.write(ready, busy_sha)
    # Publication of this artifact happens only after the canonical checkpoint.
    if provisional.is_file():
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".tmp")
        shutil.copyfile(provisional, temporary)
        temporary.replace(output)
    return code


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
    parser.add_argument("--mode", choices=["preview", "editorial", "intake", "discovery", "auto"],
                        default="preview")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
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
                               folder=folder, environment=dict(os.environ))
    except Exception as error:
        args.output.unlink(missing_ok=True)
        # HTTP exception messages can contain URLs/credentials/private response bodies.
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({"status": "completed" if code == 0 else "runtime_failed"}))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
