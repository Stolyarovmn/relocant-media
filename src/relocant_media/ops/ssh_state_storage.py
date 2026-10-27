#!/usr/bin/env python3
"""Standalone storage-only SSH helper. No application, SQLite, AI or Telegram imports."""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

MAX_HEADER = 1024 * 1024
MAX_STATE = 512 * 1024
MAX_BLOB = 50 * 1024 * 1024
CHUNK = 64 * 1024
HELPER_COMMAND = "/usr/local/libexec/relocant-media/ssh-state-storage"
TRUSTED_MARKER_UID = 0


class StorageError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def require(condition: bool, code: str = "invalid_request") -> None:
    if not condition:
        raise StorageError(code)


def hex_digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def encode(value: Any) -> bytes:
    content = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    require(len(content) <= MAX_STATE)
    return content


def sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def private_file(path: Path) -> BinaryIO:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    info = os.fstat(descriptor)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077):
        os.close(descriptor)
        raise StorageError("storage_error")
    return os.fdopen(descriptor, "rb")


class Storage:
    def __init__(self, root: Path, marker: Path) -> None:
        self.root = root
        self.marker = marker
        info = root.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) & 0o077 == 0, "storage_error")
        self.blobs = root / "blobs"
        self.blobs.mkdir(mode=0o700, exist_ok=True)
        info = self.blobs.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) & 0o077 == 0, "storage_error")
        self.pointer = root / "current.json"

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        descriptor = os.open(self.root / "state.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                             0o600)
        try:
            info = os.fstat(descriptor)
            require(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                    and stat.S_IMODE(info.st_mode) & 0o077 == 0, "storage_error")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def read_state(self) -> dict[str, Any]:
        try:
            with private_file(self.pointer) as stream:
                content = stream.read(MAX_STATE + 1)
        except FileNotFoundError:
            raise StorageError("state_missing") from None
        require(len(content) <= MAX_STATE, "storage_error")
        state = json.loads(content)
        require(isinstance(state, dict), "storage_error")
        return {"state": state, "sha": hashlib.sha256(content).hexdigest()}

    def blob(self, blob_id: Any) -> tuple[BinaryIO, int]:
        require(hex_digest(blob_id))
        try:
            stream = private_file(self.blobs / blob_id)
        except FileNotFoundError:
            raise StorageError("blob_missing") from None
        size = os.fstat(stream.fileno()).st_size
        if not 0 <= size <= MAX_BLOB:
            stream.close()
            raise StorageError("storage_error")
        digest = hashlib.sha256()
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
        if digest.hexdigest() != blob_id:
            stream.close()
            raise StorageError("storage_error")
        stream.seek(0)
        return stream, size

    def put_blob(self, sha256: Any, size: Any, incoming: BinaryIO) -> dict[str, str]:
        require(hex_digest(sha256) and type(size) is int and 0 <= size <= MAX_BLOB)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".upload-", dir=self.blobs)
        temporary = Path(temporary_name)
        try:
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "wb") as stream:
                remaining = size
                while remaining:
                    chunk = incoming.read(min(CHUNK, remaining))
                    require(bool(chunk))
                    remaining -= len(chunk)
                    digest.update(chunk)
                    stream.write(chunk)
                require(not incoming.read(1))
                require(digest.hexdigest() == sha256)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self.blobs / sha256)
            except FileExistsError:
                existing, existing_size = self.blob(sha256)
                existing.close()
                require(existing_size == size, "storage_error")
            sync_directory(self.blobs)
            return {"blob_id": sha256}
        finally:
            temporary.unlink(missing_ok=True)

    def validate_state(self, state: Any) -> bytes:
        require(isinstance(state, dict))
        require(type(state.get("version")) is int and state["version"] == 1
                and state.get("owner") == "github-actions"
                and state.get("status") in {"ready", "busy"}
                and isinstance(state.get("generation"), str) and bool(state["generation"]))
        cutover = state.get("cutover")
        require(isinstance(cutover, dict) and cutover.get("server_fenced") is True)
        snapshot = state.get("snapshot")
        require(isinstance(snapshot, dict) and hex_digest(snapshot.get("sha256"))
                and snapshot.get("drive_file_id") == snapshot["sha256"])
        stream, size = self.blob(snapshot["sha256"])
        stream.close()
        require(type(snapshot.get("bytes")) is int and snapshot["bytes"] == size)
        return encode(state)

    def install_state(self, content: bytes) -> str:
        descriptor, name = tempfile.mkstemp(prefix=".state-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.pointer)
            sync_directory(self.root)
        finally:
            temporary.unlink(missing_ok=True)
        return hashlib.sha256(content).hexdigest()

    def write_state(self, expected_sha: Any, state: Any) -> dict[str, str]:
        require(hex_digest(expected_sha))
        content = self.validate_state(state)
        with self.locked():
            recorded = self.read_state()
            require(recorded["sha"] == expected_sha, "conflict")
            old = recorded["state"]
            require(state["cutover"] == old.get("cutover"))
            if old.get("status") == "ready":
                require(state["status"] == "busy" and state.get("snapshot") == old.get("snapshot")
                        and state["generation"] == old.get("generation")
                        and isinstance(state.get("attempt"), str) and bool(state["attempt"]))
            else:
                require(old.get("status") == "busy" and state["status"] == "ready"
                        and state.get("attempt") == old.get("attempt")
                        and state["generation"] == old.get("attempt"))
            return {"sha": self.install_state(content)}

    def create_state(self, state: Any) -> dict[str, str]:
        content = self.validate_state(state)
        require(state["status"] == "ready", "bootstrap_blocked")
        with self.locked():
            try:
                self.read_state()
            except StorageError as error:
                if error.code != "state_missing":
                    raise
            else:
                raise StorageError("state_exists")
            info = self.marker.lstat()
            require(stat.S_ISREG(info.st_mode) and info.st_uid == TRUSTED_MARKER_UID
                    and stat.S_IMODE(info.st_mode) & 0o022 == 0, "bootstrap_blocked")
            descriptor = os.open(self.marker, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                marker_bytes = stream.read(MAX_HEADER + 1)
            require(len(marker_bytes) <= MAX_HEADER, "bootstrap_blocked")
            marker = json.loads(marker_bytes)
            require(isinstance(marker, dict) and marker.get("version") == 1
                    and marker.get("server_writers_fenced") is True
                    and marker.get("snapshot_sha256") == state["snapshot"]["sha256"]
                    and marker.get("snapshot_bytes") == state["snapshot"]["bytes"],
                    "bootstrap_blocked")
            return {"sha": self.install_state(content)}


def serve(storage: Storage, incoming: BinaryIO, outgoing: BinaryIO) -> None:
    header = incoming.readline(MAX_HEADER + 1)
    require(header.endswith(b"\n") and len(header) <= MAX_HEADER)
    request = json.loads(header)
    require(isinstance(request, dict))
    operation = request.get("op")
    if operation == "put_blob":
        require(set(request) == {"op", "sha256", "size"})
        result: dict[str, Any] = storage.put_blob(
            request.get("sha256"), request.get("size"), incoming)
    elif operation == "get_blob":
        require(set(request) == {"op", "blob_id"})
        stream, size = storage.blob(request.get("blob_id"))
        with stream:
            outgoing.write(encode({"sha256": request["blob_id"], "size": size}) + b"\n")
            while chunk := stream.read(CHUNK):
                outgoing.write(chunk)
        outgoing.flush()
        return
    elif operation == "read_state":
        require(set(request) == {"op"})
        result = storage.read_state()
    elif operation == "health":
        require(set(request) == {"op"})
        try:
            recorded = storage.read_state()
        except StorageError as error:
            if error.code != "state_missing":
                raise
            bootstrapped = False
        else:
            storage.validate_state(recorded["state"])
            bootstrapped = True
        result = {"storage_ready": True, "bootstrapped": bootstrapped}
    elif operation == "write_state":
        require(set(request) == {"op", "expected_sha", "state"})
        result = storage.write_state(request["expected_sha"], request["state"])
    elif operation == "create_state":
        require(set(request) == {"op", "state"})
        result = storage.create_state(request["state"])
    else:
        raise StorageError("invalid_request")
    outgoing.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    outgoing.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("/var/lib/relocant-media-state"))
    parser.add_argument("--cutover-marker", type=Path,
                        default=Path("/etc/relocant-media-state-cutover.json"))
    args = parser.parse_args()
    try:
        require(os.environ.get("SSH_ORIGINAL_COMMAND", "") in {"", HELPER_COMMAND})
        serve(Storage(args.root, args.cutover_marker), sys.stdin.buffer, sys.stdout.buffer)
    except Exception as error:
        code = error.code if isinstance(error, StorageError) else "storage_error"
        sys.stdout.buffer.write(json.dumps({"error": code}).encode() + b"\n")
        sys.stdout.buffer.flush()
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
