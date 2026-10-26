"""Hosted runner: immutable Drive backups and a fail-closed GitHub state pointer.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import google.auth
import httpx
from google.auth.transport.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.ops.newsroom_merge import merge
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.ops.sqlite_restore import restore
from relocant_media.services.cloud_discovery import batch_receipt, ingest_batch, parse_batch

REPO = "Stolyarovmn/relocant-media"
STATE_BRANCH = "editorial-state"
STATE_PATH = "state/current.json"
MAX_BYTES = 50 * 1024 * 1024


def identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,160}", value):
        raise ValueError("invalid Drive identifier")
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

    def __init__(self, client: httpx.Client, token: str) -> None:
        if not token:
            raise ValueError("GitHub state credential required")
        self.client = client
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json"}
        self.url = f"https://api.github.com/repos/{REPO}/contents/{STATE_PATH}"

    def read(self) -> tuple[dict[str, Any], str]:
        response = self.client.get(self.url, params={"ref": STATE_BRANCH}, headers=self.headers)
        response.raise_for_status()
        result = response.json()
        state = json.loads(base64.b64decode(result["content"]))
        if not isinstance(state, dict):
            raise ValueError("invalid control record")
        return state, str(result["sha"])

    def write(self, state: dict[str, Any], sha: str) -> str:
        response = self.client.put(self.url, headers=self.headers, json={
            "message": "Checkpoint hosted editorial state", "branch": STATE_BRANCH, "sha": sha,
            "content": base64.b64encode(json.dumps(state).encode()).decode(),
        })
        response.raise_for_status()
        return str(response.json()["content"]["sha"])


def validate_ready(state: dict[str, Any]) -> None:
    if (state.get("version") != 1 or state.get("owner") != "github-actions"
            or state.get("status") != "ready" or not state.get("generation")
            or state.get("cutover", {}).get("server_fenced") is not True):
        raise ValueError("canonical state not ready; never reset a busy marker automatically")
    manifest = state["snapshot"]
    identifier(manifest["drive_file_id"])
    if not re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]):
        raise ValueError("snapshot digest required")


def restored(drive: Drive, manifest: dict[str, Any], work: Path) -> Path:
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


def preview_archive(drive: Drive, manifest_id: str, work: Path, output: Path) -> int:
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


def execute(store: StateStore, drive: Drive, *, work: Path, output: Path,
            folder: str, mode: str, environment: dict[str, str]) -> int:
    if mode not in {"preview", "editorial", "intake", "discovery"}:
        raise ValueError("unsupported runtime mode")
    incoming = environment.get("CLOUD_DISCOVERY_BATCH", "")
    batch = parse_batch(incoming) if mode == "discovery" else None
    if incoming and mode != "discovery":
        raise ValueError("discovery input requires discovery mode")
    state, sha = store.read()
    validate_ready(state)
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
    for name in ("GH_STATE_TOKEN", "DRIVE_CREDENTIALS_JSON", "GOOGLE_APPLICATION_CREDENTIALS",
                 "CLOUD_DISCOVERY_BATCH"):
        env.pop(name, None)
    run_dir = work / "cycle"
    run_dir.mkdir()
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
    # Keep the projection outside _site until the ready CAS succeeds. A hard
    # termination may bypass main's exception cleanup; file presence in _site
    # must still never imply an unconfirmed checkpoint.
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
             "last_exit_code": code, "last_cycle": last_cycle}
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
    parser.add_argument("--mode", choices=["preview", "editorial", "intake", "discovery"],
                        default="preview")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=60, follow_redirects=False) as client:
            drive = Drive(client)
            if args.mode == "preview" and os.environ.get("DRIVE_PREVIEW_MANIFEST_ID"):
                code = preview_archive(drive, os.environ["DRIVE_PREVIEW_MANIFEST_ID"],
                                       args.work, args.output)
            else:
                code = execute(StateStore(client, os.environ["GH_STATE_TOKEN"]), drive,
                               work=args.work, output=args.output, mode=args.mode,
                               folder=os.environ.get("DRIVE_STATE_FOLDER_ID", ""),
                               environment=dict(os.environ))
    except Exception as error:
        args.output.unlink(missing_ok=True)
        # HTTP exception messages can contain URLs/credentials/private response bodies.
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps({"status": "completed" if code == 0 else "runtime_failed"}))
    raise SystemExit(code)


if __name__ == "__main__":
    main()
