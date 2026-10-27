"""Strict SSH transport to the storage-only helper; no editorial work runs remotely."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import stat
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_BLOB = 50 * 1024 * 1024
MAX_HEADER = 1024 * 1024
MAX_STATE = 512 * 1024
REMOTE_HELPER = "/usr/local/libexec/relocant-media/ssh-state-storage"
ERRORS = frozenset({"state_missing", "blob_missing", "state_exists", "conflict",
                    "invalid_request", "storage_error", "bootstrap_blocked"})


class SSHStorageError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in ERRORS else "storage_error"
        super().__init__(f"SSH storage blocked: {self.code}")


def _sha(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError("storage SHA256 required")
    return value


def _state_digest(state: dict[str, Any]) -> str:
    content = json.dumps(state, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                         allow_nan=False).encode()
    if len(content) > MAX_STATE:
        raise ValueError("canonical state exceeds bound")
    return hashlib.sha256(content).hexdigest()


def _file(value: str, *, private: bool) -> Path:
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise ValueError("absolute SSH credential file path required")
    path = Path(value)
    if path.is_symlink() or not path.is_file():
        raise ValueError("existing SSH credential file required")
    info = path.stat()
    if not info.st_size or not stat.S_ISREG(info.st_mode):
        raise ValueError("nonempty SSH credential file required")
    if info.st_mode & (0o077 if private else 0o022):
        raise ValueError("SSH credential file permissions are too broad")
    if private and info.st_uid != os.geteuid():
        raise ValueError("SSH private key must belong to the runner user")
    return path


@dataclass(frozen=True)
class SSHConfig:
    host: str
    user: str
    port: int
    key: Path
    known_hosts: Path

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> SSHConfig:
        host = environment.get("EDITORIAL_SSH_HOST", "")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if (not host or len(host) > 253 or any(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None
                for label in host.split(".")
            )):
                raise ValueError("valid storage SSH host required") from None
        user = environment.get("EDITORIAL_SSH_USER", "")
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user == "root":
            raise ValueError("dedicated non-root storage SSH user required")
        raw_port = environment.get("EDITORIAL_SSH_PORT", "22")
        if not re.fullmatch(r"[0-9]{1,5}", raw_port) or not 1 <= int(raw_port) <= 65535:
            raise ValueError("valid storage SSH port required")
        return cls(host, user, int(raw_port),
                   _file(environment.get("EDITORIAL_SSH_KEY_PATH", ""), private=True),
                   _file(environment.get("EDITORIAL_SSH_KNOWN_HOSTS_PATH", ""), private=False))

    def command(self) -> list[str]:
        return ["ssh", "-F", "/dev/null", "-T", "-oBatchMode=yes",
                "-oStrictHostKeyChecking=yes", f"-oUserKnownHostsFile={self.known_hosts}",
                "-oGlobalKnownHostsFile=/dev/null", "-oIdentityAgent=none", "-oIdentitiesOnly=yes",
                "-oPasswordAuthentication=no", "-oKbdInteractiveAuthentication=no",
                "-oPreferredAuthentications=publickey", "-oForwardAgent=no",
                "-oClearAllForwardings=yes", "-oConnectTimeout=15",
                "-oServerAliveInterval=15", "-oServerAliveCountMax=2",
                "-i", str(self.key), "-p", str(self.port), "--",
                f"{self.user}@{self.host}", REMOTE_HELPER]


class SSHStateClient:
    """Immutable blobs and CAS pointer share one dedicated storage-only SSH endpoint."""

    def __init__(self, config: SSHConfig) -> None:
        self.config = config

    def exchange(
        self, request: dict[str, Any], payload: bytes = b"",
    ) -> tuple[dict[str, Any], bytes]:
        header = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        if len(header) > MAX_HEADER or len(payload) > MAX_BLOB:
            raise ValueError("SSH storage request exceeds bound")
        # A private temporary file avoids buffering an entire response twice in RAM.
        # Remote diagnostics are never copied to Actions logs or exception messages.
        with tempfile.TemporaryFile() as response:
            try:
                process = subprocess.run(self.config.command(), input=header + payload,
                    stdout=response, stderr=subprocess.DEVNULL, timeout=180, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise SSHStorageError("storage_error") from None
            if response.tell() > MAX_HEADER + MAX_BLOB:
                raise SSHStorageError("storage_error")
            response.seek(0)
            raw_header = response.readline(MAX_HEADER + 1)
            if not raw_header.endswith(b"\n") or len(raw_header) > MAX_HEADER:
                raise SSHStorageError("storage_error")
            try:
                result = json.loads(raw_header)
            except (ValueError, UnicodeError):
                raise SSHStorageError("storage_error") from None
            if not isinstance(result, dict):
                raise SSHStorageError("storage_error")
            if "error" in result:
                code = result["error"]
                raise SSHStorageError(code if isinstance(code, str) and code in ERRORS
                                      else "storage_error")
            if process.returncode != 0:
                raise SSHStorageError("storage_error")
            body = response.read(MAX_BLOB + 1)
            if len(body) > MAX_BLOB:
                raise SSHStorageError("storage_error")
            return result, body

    def health(self) -> dict[str, bool]:
        result, body = self.exchange({"op": "health"})
        if (body or result.get("storage_ready") is not True
                or type(result.get("bootstrapped")) is not bool):
            raise SSHStorageError("storage_error")
        return {"storage_ready": True, "bootstrapped": result["bootstrapped"]}

    def read(self) -> tuple[dict[str, Any], str]:
        result, body = self.exchange({"op": "read_state"})
        state = result.get("state")
        if body or not isinstance(state, dict):
            raise SSHStorageError("storage_error")
        sha = _sha(result.get("sha"))
        if _state_digest(state) != sha:
            raise SSHStorageError("storage_error")
        return state, sha

    def write(self, state: dict[str, Any], sha: str) -> str:
        expected = _state_digest(state)
        result, body = self.exchange({"op": "write_state", "expected_sha": _sha(sha),
                                      "state": state})
        if body or _sha(result.get("sha")) != expected:
            raise SSHStorageError("storage_error")
        return expected

    def require_empty(self) -> None:
        try:
            self.read()
        except SSHStorageError as error:
            if error.code == "state_missing":
                return
            raise
        raise SSHStorageError("state_exists")

    def create(self, state: dict[str, Any]) -> None:
        expected = _state_digest(state)
        result, body = self.exchange({"op": "create_state", "state": state})
        if body or _sha(result.get("sha")) != expected:
            raise SSHStorageError("storage_error")
        actual, sha = self.read()
        if actual != state or sha != expected:
            raise SSHStorageError("storage_error")

    def download(self, file_id: str) -> bytes:
        result, body = self.exchange({"op": "get_blob", "blob_id": _sha(file_id)})
        if (type(result.get("size")) is not int or result["size"] != len(body)
                or _sha(result.get("sha256")) != file_id
                or hashlib.sha256(body).hexdigest() != file_id):
            raise SSHStorageError("storage_error")
        return body

    def upload(self, path: Path, folder: str) -> str:
        # folder is a legacy interface argument; SSH requests never contain filesystem paths.
        if path.stat().st_size > MAX_BLOB:
            raise ValueError("SSH snapshot exceeds bound")
        content = path.read_bytes()
        blob_id = hashlib.sha256(content).hexdigest()
        result, body = self.exchange({"op": "put_blob", "sha256": blob_id,
                                      "size": len(content)}, content)
        if body or _sha(result.get("blob_id")) != blob_id or self.download(blob_id) != content:
            raise SSHStorageError("storage_error")
        return blob_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", required=True,
                        help="check SSH storage access without bootstrap or editorial work")
    parser.parse_args()
    try:
        storage = SSHStateClient(SSHConfig.from_environment(os.environ))
        result = storage.health()
    except Exception as error:
        code = error.code if isinstance(error, SSHStorageError) else "configuration_error"
        print(json.dumps({"storage_ready": False, "error": code}))
        raise SystemExit(1) from None
    print(json.dumps(result))


if __name__ == "__main__":
    main()
