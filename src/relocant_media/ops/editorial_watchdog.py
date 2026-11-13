"""Проверка очереди независимо от расписания редакции."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.ops.actions_recovery import recover
from relocant_media.ops.actions_runtime import EDITORIAL_INTERVAL, REPO
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient, SSHStorageError

RUN_URL = f"https://github.com/{REPO}/actions/runs/"
WORKFLOW = ".github/workflows/editorial-actions.yml"
CHECKPOINT = re.compile(r'\{"recovery_checkpoint_sha256":\s*"([a-f0-9]{64})"\}')
MESSAGES = {
    "storage": "Редакция Белграда: SSH-хранилище не отвечает или вернуло ошибку чтения.",
    "busy": "Редакция Белграда остановилась. Завершённый запуск удерживает очередь, "
            "подтверждённый снимок не найден. Нужно сверить состояние перед восстановлением.",
    "recovery_failed": "Редакция Белграда остановилась. Подтверждённый снимок найден, "
                       "но восстановление не удалось. Проверьте журнал запуска.",
    "recovery_waiting_ci": "Редакция Белграда остановилась. Снимок для восстановления найден. "
                           "Монитор ждёт успешной проверки основной ветки.",
    "github": "Монитор редакции Белграда не смог проверить запуск в GitHub Actions.",
    "running_too_long": "Редакционный запуск в Белграде удерживает очередь больше 90 минут.",
    "stale": "Редакция Белграда больше часа не сохраняла редакционный цикл. "
             "Проверьте расписание и последние запуски.",
    "restored": "Очередь редакции Белграда восстановлена из проверенного снимка.",
    "healthy": "Редакция Белграда снова работает. Очередь доступна, свежий цикл сохранён.",
}


@dataclass(frozen=True)
class Check:
    status: str
    run_id: str | None = None

    @property
    def key(self) -> str:
        # Cache contains only this digest; no Telegram IDs, state, or snapshot metadata.
        return hashlib.sha256(f"{self.status}:{self.run_id}".encode()).hexdigest()


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], stderr=subprocess.DEVNULL, timeout=40).decode()


def run_metadata(run_id: str) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(gh("api", f"repos/{REPO}/actions/runs/{run_id}")))


def checkpoint_from_run(run_id: str, attempt: str) -> str | None:
    jobs = json.loads(gh("api", f"repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs"))
    job_ids = [str(job["id"]) for job in jobs["jobs"] if job["name"] == "cycle"]
    if len(job_ids) != 1 or not job_ids[0].isdigit():
        return None
    log = gh("run", "view", run_id, "--repo", REPO, "--attempt", attempt, "--job", job_ids[0],
             "--log")
    matches = set(CHECKPOINT.findall(log))
    return matches.pop() if len(matches) == 1 else None


def inspect(
    client: SSHStateClient, now: datetime, work: Path, *, allow_recovery: bool,
) -> Check:
    try:
        state, _ = client.read()
    except SSHStorageError:
        return Check("storage")
    if state.get("status") == "ready":
        last = state.get("last_editorial_at") or state.get("cutover", {}).get("at")
        if last and now - datetime.fromisoformat(last) >= 2 * EDITORIAL_INTERVAL:
            return Check("stale")
        return Check("healthy")
    run_id, attempt = state.get("run_id"), state.get("run_attempt")
    if (state.get("status") != "busy" or not isinstance(run_id, str)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)
            or not isinstance(attempt, str) or not re.fullmatch(r"[1-9][0-9]{0,5}", attempt)):
        return Check("busy")
    try:
        run = run_metadata(run_id)
    except subprocess.SubprocessError:
        return Check("github", run_id)
    if (str(run["id"]) != run_id or str(run["run_attempt"]) != attempt
            or run["path"] != WORKFLOW or run["head_branch"] != "main"):
        return Check("busy", run_id)
    if run["status"] != "completed":
        age = now - datetime.fromisoformat(state["started_at"])
        return Check("running_too_long" if age >= timedelta(minutes=90) else "running", run_id)
    try:
        checkpoint = checkpoint_from_run(run_id, attempt)
    except subprocess.SubprocessError:
        return Check("github", run_id)
    if checkpoint is None:
        return Check("busy", run_id)
    if not allow_recovery:
        return Check("recovery_waiting_ci", run_id)
    try:
        recover(client, run_id, checkpoint, work)
    except Exception:
        return Check("recovery_failed", run_id)
    return Check("restored", run_id)


def should_notify(check: Check, previous: str) -> bool:
    if check.status == "running" or check.key == previous:
        return False
    if check.status == "healthy":
        return bool(previous and previous != Check("healthy").key)
    return True


async def notify_owner(check: Check) -> None:
    chat_id = int(os.environ["EDITORIAL_OWNER_CHAT_ID"])
    if chat_id <= 0 or chat_id != int(os.environ["EDITORIAL_OWNER_USER_ID"]):
        raise ValueError("positive private owner chat required")
    link = RUN_URL + check.run_id if check.run_id else (
        f"https://github.com/{REPO}/actions/workflows/editorial-actions.yml"
    )
    async with httpx.AsyncClient(timeout=18, trust_env=False) as http:
        bot = TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], http)
        await bot.send_message(chat_id, f"{MESSAGES[check.status]}\n{link}")


def current_main_passed_ci() -> bool:
    sha = os.environ["GITHUB_SHA"]
    try:
        main = gh("api", f"repos/{REPO}/git/ref/heads/main", "--jq", ".object.sha").strip()
        ci = gh("run", "list", "--repo", REPO, "--workflow", "ci.yml", "--branch", "main",
                "--event", "push", "--commit", sha, "--limit", "1", "--json", "conclusion")
    except subprocess.SubprocessError:
        return False
    return sha == main and json.loads(ci) == [{"conclusion": "success"}]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        client = SSHStateClient(SSHConfig.from_environment(os.environ))
        check = inspect(client, datetime.now(UTC), args.work,
                        allow_recovery=not args.dry_run and current_main_passed_ci())
        print(json.dumps({"watchdog": check.status, "run_id": check.run_id,
                          "dry_run": args.dry_run}))
        if args.dry_run or check.status == "running":
            return
        previous = args.state.read_text(encoding="utf-8") if args.state.exists() else ""
        if should_notify(check, previous):
            asyncio.run(notify_owner(check))
        saved = Check("healthy").key if check.status == "restored" else check.key
        if saved != previous:
            args.state.write_text(saved, encoding="utf-8")
            with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
                output.write("changed=true\n")
    except Exception as error:
        print(json.dumps({"watchdog_error": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
