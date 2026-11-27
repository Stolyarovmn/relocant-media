"""Small process lock shared by the listener, worker and deployment tooling."""
import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


def runtime_lock_dir(state_dir: Path) -> Path:
    """Keep local locks by their state unless a server unit configures a shared directory."""
    override = os.environ.get("RELOCANT_RUNTIME_LOCK_DIR")
    return Path(override) if override else state_dir


@contextmanager
def file_lock(path: Path, *, blocking: bool = False) -> Iterator[bool]:
    with path.open("a+b") as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        module = importlib.import_module("msvcrt" if os.name == "nt" else "fcntl")
        try:
            if os.name == "nt":
                mode = module.LK_LOCK if blocking else module.LK_NBLCK
                module.locking(handle.fileno(), mode, 1)
            else:
                mode = module.LOCK_EX | (0 if blocking else module.LOCK_NB)
                module.flock(handle.fileno(), mode)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                module.locking(handle.fileno(), module.LK_UNLCK, 1)
            else:
                module.flock(handle.fileno(), module.LOCK_UN)
