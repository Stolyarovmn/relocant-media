"""Проверяемый перенос приёма Telegram в Vercel под общей очередью Actions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from relocant_media.ai.package import canonical_json
from relocant_media.ops.actions_runtime import REPO, restored, validate_ready
from relocant_media.ops.serverless_snapshot import export_intake_state
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID

PRODUCTION_ORIGIN = "https://relocant-reader-intake.vercel.app"
BOT_USERNAME = "relocant_media_belgrade_bot"
MAX_SEED_BYTES = 512 * 1024


def _phase(name: str) -> None:
    print(json.dumps({"ingress_cutover_phase": name}), file=sys.stderr, flush=True)


def _api(client: httpx.Client, token: str, method: str, payload: dict[str, Any]) -> Any:
    if (method not in {"getMe", "getChatMember", "getWebhookInfo", "setWebhook"}
            or not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,100}", token)):
        raise ValueError("Недопустимый вызов Bot API при переносе")
    response = client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload,
                           timeout=30, follow_redirects=False)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("ok") is not True:
        raise ValueError("Telegram не подтвердил операцию переноса")
    return body.get("result")


def _export(storage: SSHStateClient, state: dict[str, Any], work: Path,
            now: datetime, bot_id: int) -> dict[str, Any]:
    database = restored(storage, state["snapshot"], work)
    engine = create_engine(f"sqlite:///file:{database.as_posix()}?mode=ro&uri=true")
    try:
        with Session(engine) as session:
            session.execute(text("PRAGMA query_only=ON"))
            return export_intake_state(session, now, bot_id=bot_id)
    finally:
        engine.dispose()


def _pending_notices(seed: dict[str, Any]) -> int:
    return sum(notice.get("status") in {"pending", "sending", "uncertain"}
               for case in seed["cases"]
               for notice in case["payload"].get("reader_notices", {}).values())


def _transfer(client: httpx.Client, url: str, bridge: str,
              payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post(f"{url}/api/transfer", json=payload,
                           headers={"Authorization": f"Bearer {bridge}"},
                           timeout=30, follow_redirects=False)
    response.raise_for_status()
    if len(response.content) > MAX_SEED_BYTES:
        raise ValueError("Ответ приёмника превышает предел")
    body = response.json()
    if not isinstance(body, dict):
        raise ValueError("Некорректное подтверждение приёмника")
    return cast(dict[str, Any], body)


def execute(storage: SSHStateClient, url: str, work: Path, client: httpx.Client,
            token: str, bridge_token: str, webhook_secret: str, *, activate: bool = False,
            now: datetime | None = None) -> dict[str, Any]:
    if url != PRODUCTION_ORIGIN:
        raise ValueError("Перенос разрешён только в отдельный проект приёмника")
    at = now or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("Время переноса должно содержать часовой пояс")
    if activate and not all(re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value)
                            for value in (bridge_token, webhook_secret)):
        raise ValueError("Требуются отдельные служебные ключи приёмника")
    _phase("verify_bot")
    me = _api(client, token, "getMe", {})
    if (not isinstance(me, dict) or me.get("is_bot") is not True
            or me.get("username") != BOT_USERNAME or type(me.get("id")) is not int):
        raise ValueError("Токен не относится к рабочему редакционному боту")
    member = _api(client, token, "getChatMember", {
        "chat_id": APPROVED_CHANNEL_ID, "user_id": me["id"],
    })
    if not isinstance(member, dict) or member.get("can_manage_direct_messages") is not True:
        raise ValueError("Нет доступа к сообщениям основного канала")
    current_webhook = _api(client, token, "getWebhookInfo", {})
    destination = f"{url}/api/telegram"
    if (not isinstance(current_webhook, dict)
            or current_webhook.get("url", "") not in {"", destination}):
        raise ValueError("У бота уже есть другой обработчик обновлений")
    _phase("read_canonical_state")
    state, state_sha = storage.read()
    validate_ready(state)
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    existing = state.get("intake_state", {}).get("ingress")
    if existing:
        if (existing.get("backend") != "vercel" or existing.get("url") != url
                or existing.get("phase") not in {"prepared", "active"}):
            raise ValueError("В каноническом состоянии указан другой перенос")
        seed_sha = existing["seed_sha256"]
        seed_bytes = storage.download(seed_sha)
        if (len(seed_bytes) > MAX_SEED_BYTES
                or hashlib.sha256(seed_bytes).hexdigest() != seed_sha):
            raise ValueError("Сохранённый снимок предложки повреждён")
        seed = json.loads(seed_bytes)
    else:
        if current_webhook.get("url"):
            raise ValueError("Webhook включён без сохранённой подготовки переноса")
        _phase("restore_verified_snapshot")
        seed = _export(storage, state, work, at, me["id"])
        seed_bytes = canonical_json(seed).encode()
        seed_sha = hashlib.sha256(seed_bytes).hexdigest()
    if (len(seed_bytes) > MAX_SEED_BYTES or seed.get("version") != 1
            or seed.get("bot_id") != me["id"] or seed.get("channel_id") != APPROVED_CHANNEL_ID
            or not isinstance(seed.get("cases"), list)):
        raise ValueError("Неподходящий снимок предложки")
    summary = {"seed_sha256": seed_sha, "cases": len(seed["cases"]),
               "open_drafts": sum(case["status"] in {"choosing", "collecting"}
                                  for case in seed["cases"]),
               "processed_updates": len(seed["processed_updates"]),
               "unresolved_notices": _pending_notices(seed)}
    if not activate:
        return {"status": "preflight_ready" if not summary["unresolved_notices"]
                else "preflight_blocked_notices", **summary,
                "webhook_already_target": current_webhook.get("url") == destination}
    if summary["unresolved_notices"]:
        raise ValueError("Сначала требуется проверить незавершённые ответы читателям")
    if not existing:
        _phase("save_seed_and_fence_polling")
        seed_path = work / "intake-seed.json"
        seed_path.write_bytes(seed_bytes)
        if storage.upload(seed_path, "intake-seed") != seed_sha:
            raise ValueError("Хранилище не подтвердило снимок предложки")
        metadata = {"version": 1, "backend": "vercel", "phase": "prepared", "url": url,
                    "seed_sha256": seed_sha, "seed_snapshot_sha256": state["snapshot"]["sha256"],
                    "created_at": at.isoformat()}
        state = {**state, "generation": uuid.uuid4().hex, "intake_state": {
            **state.get("intake_state", {}), "ingress": metadata,
        }}
        state_sha = storage.write(state, state_sha)
    _phase("bootstrap_private_ingress")
    result = _transfer(client, url, bridge_token, {
        "operation": "bootstrap", "seed_json": seed_bytes.decode(), "seed_sha256": seed_sha,
        "bot_token": token, "webhook_secret": webhook_secret, "mode": "active",
    })
    if (result.get("status") != "ready" or result.get("seed_sha256") != seed_sha
            or result.get("can_send") is not True):
        raise ValueError("Приёмник не подтвердил готовность рабочего режима")
    _phase("configure_webhook")
    if _api(client, token, "setWebhook", {
        "url": destination, "secret_token": webhook_secret,
        "max_connections": 1, "drop_pending_updates": False,
    }) is not True:
        raise ValueError("Telegram не подтвердил переключение приёмника")
    _phase("verify_webhook")
    verified = _api(client, token, "getWebhookInfo", {})
    if (not isinstance(verified, dict) or verified.get("url") != destination
            or verified.get("max_connections") != 1):
        raise ValueError("Webhook после переключения не совпадает с планом")
    fresh, fresh_sha = storage.read()
    validate_ready(fresh)
    if (fresh, fresh_sha) != (state, state_sha):
        raise ValueError("Каноническое состояние изменилось во время переключения")
    active = {**fresh, "generation": uuid.uuid4().hex, "intake_state": {
        **fresh["intake_state"], "ingress": {**fresh["intake_state"]["ingress"],
            "phase": "active", "activated_at": at.isoformat()},
    }}
    _phase("activate_canonical_ingress")
    storage.write(active, fresh_sha)
    return {"status": "active", **summary, "origin": url,
            "pending_update_count": verified.get("pending_update_count", 0)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("preflight", "activate"), default="preflight")
    parser.add_argument("--url", default=PRODUCTION_ORIGIN)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    try:
        if (os.environ.get("GITHUB_ACTIONS") != "true"
                or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
                or os.environ.get("GITHUB_REPOSITORY") != REPO
                or os.environ.get("GITHUB_REF") != "refs/heads/main"
                or os.environ.get("EDITORIAL_STORAGE_BACKEND") != "ssh"
                or os.environ.get("ACTIONS_CUTOVER_CONFIRMED") != "true"):
            raise ValueError("Перенос разрешён только ручным Actions-запуском main")
        storage = SSHStateClient(SSHConfig.from_environment(os.environ))
        with httpx.Client(trust_env=False, transport=httpx.HTTPTransport(retries=0)) as client:
            result = execute(storage, args.url, args.work, client,
                             os.environ["TELEGRAM_BOT_TOKEN"],
                             os.environ.get("EDITORIAL_INGRESS_TOKEN", ""),
                             os.environ.get("EDITORIAL_INGRESS_WEBHOOK_SECRET", ""),
                             activate=args.operation == "activate")
    except Exception as error:
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result))
    if result["status"] == "preflight_blocked_notices":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
