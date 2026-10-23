import os
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).parents[1] / "deploy/server/check-main-sync.sh"


def _git(cwd: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, str]:
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    work.mkdir()
    remote.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "Runtime Test")
    (work / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-m", "initial")
    initial = _git(work, "rev-parse", "HEAD")
    _git(remote, "init", "--bare")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main")
    return work, remote, initial


def _check(work: pathlib.Path, remote: pathlib.Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "RELOCANT_APP_DIR": str(work),
        "RELOCANT_GIT_REMOTE_URL": str(remote),
        "RELOCANT_GIT_BRANCH": "main",
    }
    return subprocess.run(
        ["/bin/sh", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_runtime_freshness_accepts_exact_remote_main(tmp_path: pathlib.Path) -> None:
    work, remote, initial = _repository(tmp_path)

    result = _check(work, remote)

    assert result.returncode == 0
    assert initial in result.stdout
    assert "verified" in result.stdout


def test_runtime_freshness_rejects_stale_checkout(tmp_path: pathlib.Path) -> None:
    work, remote, initial = _repository(tmp_path)
    (work / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-m", "new main")
    newest = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    _git(work, "reset", "--hard", initial)

    result = _check(work, remote)

    assert result.returncode == 78
    assert "runtime is stale" in result.stderr
    assert f"deployed={initial}" in result.stderr
    assert f"expected={newest}" in result.stderr


def test_runtime_freshness_rejects_tracked_modifications(tmp_path: pathlib.Path) -> None:
    work, remote, _ = _repository(tmp_path)
    (work / "tracked.txt").write_text("locally changed\n", encoding="utf-8")

    result = _check(work, remote)

    assert result.returncode == 78
    assert "tracked modifications" in result.stderr
