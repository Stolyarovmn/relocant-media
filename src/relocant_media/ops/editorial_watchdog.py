"""Проверка очереди независимо от расписания редакции."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.ops.actions_recovery import recover
from relocant_media.ops.actions_runtime import EDITORIAL_INTERVAL, REPO
from relocant_media.ops.belgrade_digests import (
    MorningDelivery,
    digest_end_warning,
    morning_delivery,
)
from relocant_media.ops.editorial_failure_streak import (
    ALERT_THRESHOLD,
    CAUSES,
    STREAK_LIMIT,
    FailureStreak,
    recent_failure_streak,
)
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient, SSHStorageError

RUN_URL = f"https://github.com/{REPO}/actions/runs/"
WORKFLOW = ".github/workflows/editorial-actions.yml"
CHECKPOINT = re.compile(r'\{"recovery_checkpoint_sha256":\s*"([a-f0-9]{64})"\}')
MESSAGES = {
    "storage": "Редакция Белграда: SSH-хранилище не отвечает или вернуло ошибку чтения.",
    "storage_capacity": "У хранилища редакции Белграда мало свободного места. "
                        "Проверьте запас на диске и резервные копии до остановки выпуска.",
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
MORNING_REASONS = {
    "insufficient_items": "Редакционный цикл зафиксировал, что для выпуска не хватило материалов.",
    "window_closed": "Окно догоняющего выпуска закрылось до отправки.",
    "telegram_unconfirmed": "Telegram не подтвердил результат отправки. Сверьте выпуск в канале.",
    "invalid_receipt": "Запись об отправке неполная. Сверьте выпуск в канале.",
    "no_recorded_result": "В журнале редакции нет записи о результате попытки. "
                          "Проверьте последние запуски.",
}


@dataclass(frozen=True)
class Check:
    status: str
    run_id: str | None = None
    streak: FailureStreak | None = None
    morning: MorningDelivery | None = None
    digest_end: datetime | None = None

    @property
    def key(self) -> str:
        # Alert cache keys carry no Telegram IDs or snapshot metadata.
        identity = self.streak.cause if self.streak else self.run_id
        return hashlib.sha256(f"{self.status}:{identity}".encode()).hexdigest()


@dataclass(frozen=True)
class AlertState:
    queue: str = ""
    morning: str = ""
    digest_end: str = ""

    @classmethod
    def read(cls, path: Path) -> AlertState:
        if not path.is_file():
            return cls()
        text = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[a-f0-9]{64}", text):
            return cls(queue=text)
        try:
            value = json.loads(text)
        except ValueError:
            return cls()
        if not isinstance(value, dict):
            return cls()
        return cls(**{key: stamp for key in ("queue", "morning", "digest_end")
                      if isinstance(stamp := value.get(key), str)
                      and re.fullmatch(r"[a-f0-9]{64}", stamp)})


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
    client: SSHStateClient, now: datetime, work: Path, *,
    allow_recovery: bool, check_digests: bool = False, morning_date: date | None = None,
) -> Check:
    end = digest_end_warning(now) if check_digests and morning_date is None else None
    try:
        state, _, capacity_low = client.read_status()
    except SSHStorageError:
        if morning_date is not None:
            raise
        return Check("storage", digest_end=end)
    if morning_date is not None:
        morning = morning_delivery(state, now) if check_digests else None
        if morning is None or morning.slot.scheduled.date() != morning_date:
            return Check("morning_only")
        return Check("morning_only", morning=morning)
    result = _inspect_queue(client, state, capacity_low, now, work, allow_recovery=allow_recovery)
    if check_digests and result.status != "restored":
        return replace(result, morning=morning_delivery(state, now), digest_end=end)
    return replace(result, digest_end=end) if end is not None else result


def _inspect_queue(
    client: SSHStateClient, state: dict[str, Any], capacity_low: bool | None,
    now: datetime, work: Path, *, allow_recovery: bool,
) -> Check:
    if state.get("status") == "ready":
        if capacity_low:
            return Check("storage_capacity")
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
        if age >= timedelta(minutes=90):
            return Check("running_too_long", run_id)
        return Check("storage_capacity") if capacity_low else Check("running", run_id)
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
    if check.status in {"running", "morning_only"} or check.key == previous:
        return False
    if check.status == "healthy":
        return bool(previous and previous != Check("healthy").key)
    return True


def check_failure_streak(check: Check) -> Check:
    if check.status in {"healthy", "stale", "storage", "storage_capacity", "busy"}:
        streak = recent_failure_streak(REPO)
        if streak and streak.count >= ALERT_THRESHOLD:
            return replace(check, status="repeated_failure", run_id=streak.run_id, streak=streak)
    return check


def notification(check: Check) -> str:
    if check.streak:
        amount = str(check.streak.count)
        if check.streak.count >= STREAK_LIMIT:
            amount = f"не менее {STREAK_LIMIT}"
        return (f"Серия сбоев редакции Белграда. Число сбоев подряд — {amount}. "
                f"Причина — {CAUSES[check.streak.cause]}.")
    return MESSAGES[check.status]


async def notify_owner(
    check: Check | None, morning: MorningDelivery | None = None,
    digest_end: datetime | None = None,
) -> None:
    chat_id = int(os.environ["EDITORIAL_OWNER_CHAT_ID"])
    if chat_id <= 0 or chat_id != int(os.environ["EDITORIAL_OWNER_USER_ID"]):
        raise ValueError("positive private owner chat required")
    link = RUN_URL + check.run_id if check and check.run_id else (
        f"https://github.com/{REPO}/actions/workflows/editorial-actions.yml"
    )
    messages = [notification(check)] if check else []
    if morning is not None:
        status = ("отправка не подтверждена" if morning.status == "unconfirmed"
                  else f"пропущена к {morning.slot.expires:%H:%M} по Белграду")
        messages.append(f"Утренняя сводка за {morning.slot.scheduled:%d.%m.%Y} — {status}. "
                        + MORNING_REASONS[morning.reason])
    if digest_end is not None:
        messages.append(
            f"В расписании сводок задано окончание выпуска {digest_end:%d.%m.%Y в %H:%M} "
            "по Белграду. Чтобы продолжить выпуски, измените срок в настройках расписания."
        )
    messages.append(link)
    async with httpx.AsyncClient(timeout=18, trust_env=False) as http:
        bot = TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], http)
        await bot.send_message(chat_id, "\n\n".join(messages))


def update_alerts(check: Check, path: Path) -> bool:
    previous = AlertState.read(path)
    queue = check if should_notify(check, previous.queue) else None
    morning = check.morning
    morning_key = previous.morning
    if morning is not None and morning.status != "sent":
        morning_key = hashlib.sha256(morning.slot.key.encode()).hexdigest()
        if morning_key == previous.morning:
            morning = None
    else:
        morning = None
    digest_end = check.digest_end
    end_key = previous.digest_end
    if digest_end is not None:
        end_key = hashlib.sha256(digest_end.isoformat().encode()).hexdigest()
        if end_key == previous.digest_end:
            digest_end = None
    if queue is not None or morning is not None or digest_end is not None:
        asyncio.run(notify_owner(queue, morning, digest_end))
    queue_key = (previous.queue if check.status in {"running", "morning_only"}
                 else Check("healthy").key if check.status == "restored" else check.key)
    saved = AlertState(queue=queue_key, morning=morning_key, digest_end=end_key)
    if saved == previous:
        return False
    path.write_text(json.dumps(asdict(saved)), encoding="utf-8")
    return True


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
    parser.add_argument("--morning-date", type=date.fromisoformat)
    args = parser.parse_args()
    try:
        client = SSHStateClient(SSHConfig.from_environment(os.environ))
        check = inspect(client, datetime.now(UTC), args.work,
                        allow_recovery=(args.morning_date is None and not args.dry_run
                                        and current_main_passed_ci()),
                        check_digests=(os.getenv("EDITORIAL_SEND_ENABLED") == "true"
                                       and os.getenv("EDITORIAL_CHANNEL_MODE") == "public"),
                        morning_date=args.morning_date)
        check = check_failure_streak(check)
        morning = check.morning
        print(json.dumps({"watchdog": check.status, "run_id": check.run_id,
                          "morning": {"slot": morning.slot.key, "status": morning.status,
                                      "reason": morning.reason} if morning else None,
                          "digest_end": check.digest_end.isoformat() if check.digest_end else None,
                          "dry_run": args.dry_run}))
        if args.dry_run:
            return
        if update_alerts(check, args.state):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
                output.write("changed=true\n")
    except Exception as error:
        print(json.dumps({"watchdog_error": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
