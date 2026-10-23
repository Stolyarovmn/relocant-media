import os
import pathlib
import stat
import subprocess
import sys

ROOT = pathlib.Path(__file__).parents[1]
SCRIPT = ROOT / "deploy/server/deploy-green-main.sh"
DEPLOY_SERVICE = ROOT / "deploy/server/relocant-deploy.service"


def _git(cwd: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _write_executable(path: pathlib.Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, str, str]:
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    work.mkdir()
    remote.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "test@example.invalid")
    _git(work, "config", "user.name", "Deploy Test")

    deploy = work / "deploy/server"
    deploy.mkdir(parents=True)
    for unit in (
        "relocant-editorial.service",
        "relocant-editorial.timer",
        "relocant-intake.service",
        "relocant-intake.timer",
        "relocant-deploy.service",
        "relocant-deploy.timer",
    ):
        (deploy / unit).write_text(f"# {unit}\n", encoding="utf-8")
    (deploy / "deploy-green-main.sh").write_text(
        SCRIPT.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (deploy / "check-main-sync.sh").write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "actual=$(git rev-parse HEAD)\n"
        "expected=$(git ls-remote --exit-code \"$RELOCANT_GIT_REMOTE_URL\" "
        "refs/heads/main | awk 'NR == 1 {print $1}')\n"
        "[ \"$actual\" = \"$expected\" ]\n",
        encoding="utf-8",
    )
    (work / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "initial")
    initial = _git(work, "rev-parse", "HEAD")

    _git(remote, "init", "--bare")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-u", "origin", "main")

    (work / "tracked.txt").write_text("two\n", encoding="utf-8")
    _git(work, "add", "tracked.txt")
    _git(work, "commit", "-m", "green target")
    target = _git(work, "rev-parse", "HEAD")
    _git(work, "push", "origin", "main")
    _git(work, "reset", "--hard", initial)
    return work, remote, initial, target


def _mocks(
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path, pathlib.Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    systemctl_log = tmp_path / "systemctl.log"
    systemctl_log.write_text("", encoding="utf-8")
    systemctl = bin_dir / "systemctl"
    _write_executable(
        systemctl,
        "#!/bin/sh\n"
        "if [ -n \"${MOCK_SYSTEMCTL_LOG:-}\" ]; then "
        "printf '%s\\n' \"$*\" >> \"$MOCK_SYSTEMCTL_LOG\"; fi\n"
        "if [ \"${1:-}\" = is-active ]; then exit 1; fi\n"
        "exit 0\n",
    )
    curl = bin_dir / "curl"
    _write_executable(
        curl,
        "#!/bin/sh\n"
        "url=''\n"
        "for arg in \"$@\"; do case \"$arg\" in https://*) url=\"$arg\";; esac; done\n"
        "sha=$(printf '%s' \"$url\" | sed -n 's/.*head_sha=\\([^&]*\\).*/\\1/p')\n"
        "conclusion=${MOCK_CI_CONCLUSION:-success}\n"
        "path=${MOCK_CI_PATH:-.github/workflows/ci.yml}\n"
        "printf '{\"workflow_runs\":[{\"name\":\"ci\",\"path\":\"%s\","
        "\"head_sha\":\"%s\",\"head_branch\":\"main\",\"event\":\"push\","
        "\"status\":\"completed\",\"conclusion\":\"%s\"}]}' "
        "\"$path\" \"$sha\" \"$conclusion\"\n",
    )
    venv_python = bin_dir / "venv-python"
    _write_executable(venv_python, "#!/bin/sh\nexit 0\n")
    return systemctl, curl, venv_python, systemctl_log


def _run(
    work: pathlib.Path,
    remote: pathlib.Path,
    tmp_path: pathlib.Path,
    *,
    ci_conclusion: str = "success",
    ci_path: str = ".github/workflows/ci.yml",
) -> tuple[subprocess.CompletedProcess[str], pathlib.Path]:
    systemd_dir = tmp_path / "systemd"
    systemd_dir.mkdir(exist_ok=True)
    deploy_libexec_dir = tmp_path / "libexec"
    systemctl, curl, venv_python, systemctl_log = _mocks(tmp_path)
    env = {
        **os.environ,
        "RELOCANT_DEPLOY_TEST_MODE": "true",
        "RELOCANT_APP_DIR": str(work),
        "RELOCANT_GIT_REMOTE_URL": str(remote),
        "RELOCANT_GIT_BRANCH": "main",
        "RELOCANT_WORKER_LOCK": str(tmp_path / "worker.lock"),
        "RELOCANT_SYSTEMD_DIR": str(systemd_dir),
        "RELOCANT_DEPLOY_LIBEXEC_DIR": str(deploy_libexec_dir),
        "RELOCANT_SYSTEMCTL_BIN": str(systemctl),
        "RELOCANT_CURL_BIN": str(curl),
        "RELOCANT_PYTHON_BIN": sys.executable,
        "RELOCANT_VENV_PYTHON": str(venv_python),
        "MOCK_CI_CONCLUSION": ci_conclusion,
        "MOCK_CI_PATH": ci_path,
        "MOCK_SYSTEMCTL_LOG": str(systemctl_log),
    }
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
    )
    return result, systemctl_log


def _systemctl_calls(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def test_deploy_fast_forwards_only_green_exact_main(tmp_path: pathlib.Path) -> None:
    work, remote, _, target = _repository(tmp_path)

    result, systemctl_log = _run(work, remote, tmp_path)

    assert result.returncode == 0, result.stderr
    assert _git(work, "rev-parse", "HEAD") == target
    assert "deployed green main:" in result.stdout
    systemd_dir = tmp_path / "systemd"
    assert (systemd_dir / "relocant-editorial.service").is_file()
    assert (systemd_dir / "relocant-intake.timer").is_file()
    assert (systemd_dir / "relocant-deploy.service").is_file()
    assert (systemd_dir / "relocant-deploy.timer").is_file()
    installed_script = tmp_path / "libexec/deploy-green-main.sh"
    assert installed_script.read_text(encoding="utf-8") == (
        work / "deploy/server/deploy-green-main.sh"
    ).read_text(encoding="utf-8")
    assert installed_script.stat().st_mode & stat.S_IXUSR
    calls = _systemctl_calls(systemctl_log)
    assert "stop relocant-editorial.timer relocant-intake.timer" in calls
    assert "enable --now relocant-deploy.timer" in calls


def test_exact_green_bootstrap_installs_watcher_without_stopping_editorial(
    tmp_path: pathlib.Path,
) -> None:
    work, remote, _, target = _repository(tmp_path)
    _git(work, "reset", "--hard", target)

    result, systemctl_log = _run(work, remote, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "installed green deployment watcher" in result.stdout
    calls = _systemctl_calls(systemctl_log)
    assert "stop relocant-editorial.timer" not in calls
    assert "enable --now relocant-deploy.timer" in calls
    assert (tmp_path / "libexec/deploy-green-main.sh").is_file()


def test_deploy_noop_does_not_touch_systemd_after_control_plane_is_current(
    tmp_path: pathlib.Path,
) -> None:
    work, remote, _, _ = _repository(tmp_path)
    first, _ = _run(work, remote, tmp_path)
    assert first.returncode == 0, first.stderr

    result, systemctl_log = _run(work, remote, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "already matches main" in result.stdout
    assert _systemctl_calls(systemctl_log) == ""


def test_deploy_refuses_target_without_successful_exact_ci(tmp_path: pathlib.Path) -> None:
    work, remote, initial, target = _repository(tmp_path)

    result, systemctl_log = _run(work, remote, tmp_path, ci_conclusion="failure")

    assert result.returncode == 78
    assert _git(work, "rev-parse", "HEAD") == initial
    assert target in result.stderr
    assert "completed:failure" in result.stderr
    assert _systemctl_calls(systemctl_log) == ""


def test_deploy_refuses_named_ci_from_noncanonical_workflow(tmp_path: pathlib.Path) -> None:
    work, remote, initial, target = _repository(tmp_path)

    result, systemctl_log = _run(
        work,
        remote,
        tmp_path,
        ci_path=".github/workflows/not-production-ci.yml",
    )

    assert result.returncode == 78
    assert _git(work, "rev-parse", "HEAD") == initial
    assert target in result.stderr
    assert "missing:missing" in result.stderr
    assert _systemctl_calls(systemctl_log) == ""


def test_deploy_refuses_non_fast_forward_remote_main(tmp_path: pathlib.Path) -> None:
    work, remote, initial, _ = _repository(tmp_path)
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    _git(other, "config", "user.email", "test@example.invalid")
    _git(other, "config", "user.name", "Deploy Test")
    _git(other, "checkout", "--orphan", "replacement")
    (other / "replacement.txt").write_text("replacement\n", encoding="utf-8")
    _git(other, "add", ".")
    _git(other, "commit", "-m", "replacement main")
    replacement = _git(other, "rev-parse", "HEAD")
    _git(other, "push", "--force", "origin", "HEAD:main")

    result, systemctl_log = _run(work, remote, tmp_path)

    assert result.returncode == 78
    assert _git(work, "rev-parse", "HEAD") == initial
    assert replacement in result.stderr
    assert "non-fast-forward" in result.stderr
    assert _systemctl_calls(systemctl_log) == ""


def test_deploy_refuses_dirty_checkout_before_timer_changes(tmp_path: pathlib.Path) -> None:
    work, remote, initial, _ = _repository(tmp_path)
    (work / "tracked.txt").write_text("dirty\n", encoding="utf-8")

    result, systemctl_log = _run(work, remote, tmp_path)

    assert result.returncode == 78
    assert _git(work, "rev-parse", "HEAD") == initial
    assert "not clean" in result.stderr
    assert _systemctl_calls(systemctl_log) == ""


def test_root_deploy_service_executes_only_installed_root_owned_script() -> None:
    service = DEPLOY_SERVICE.read_text(encoding="utf-8")
    checkout_exec = (
        "ExecStart=/bin/sh /opt/relocant-media/app/deploy/server/deploy-green-main.sh"
    )

    assert "ExecStart=/usr/local/libexec/relocant-media/deploy-green-main.sh" in service
    assert checkout_exec not in service
    assert "NoNewPrivileges=true" in service
