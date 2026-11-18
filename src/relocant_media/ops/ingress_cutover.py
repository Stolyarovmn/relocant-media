"""Проверяемый перенос приёма Telegram в Vercel под общей очередью Actions."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from relocant_media.ai.package import canonical_json
from relocant_media.ops.actions_runtime import REPO, _commit_republication, restored, validate_ready
from relocant_media.ops.serverless_snapshot import export_intake_state
from relocant_media.ops.ssh_state_client import SSHConfig, SSHStateClient
from relocant_media.ops.vercel_inbox import ingress_config, sequence_cursor
from relocant_media.services.channel_delivery import APPROVED_CHANNEL_ID

PRODUCTION_ORIGIN = "https://relocant-reader-intake.vercel.app"
BOT_USERNAME = "relocant_media_belgrade_bot"
MAX_SEED_BYTES = 512 * 1024
ROLLBACK_QUIESCE_SECONDS = 45


def _phase(name: str) -> None:
    print(json.dumps({"ingress_cutover_phase": name}), file=sys.stderr, flush=True)


def _api(client: httpx.Client, token: str, method: str, payload: dict[str, Any]) -> Any:
    if (method not in {"getMe", "getChatMember", "getWebhookInfo", "setWebhook", "deleteWebhook"}
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


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Не указано проверяемое время состояния приёмника")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Некорректное время состояния приёмника") from None
    if result.tzinfo is None:
        raise ValueError("Время состояния приёмника должно содержать часовой пояс")
    return result


def _activation_evidence(storage: SSHStateClient, digest: str, run_id: str,
                         source: dict[str, Any]) -> datetime:
    if (not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not re.fullmatch(r"[1-9][0-9]{0,19}", run_id)):
        raise ValueError("Нужен проверенный checkpoint успешного ручного переключения")
    content = storage.download(digest)
    if len(content) > MAX_SEED_BYTES or hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("Checkpoint переключения повреждён")
    evidence = json.loads(content)
    if (not isinstance(evidence, dict)
            or set(evidence) != {"version", "expected_busy_sha256", "state"}
            or type(evidence.get("version")) is not int or evidence["version"] != 1
            or not isinstance(evidence.get("expected_busy_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", evidence["expected_busy_sha256"])
            or not isinstance(evidence.get("state"), dict)):
        raise ValueError("Неподходящий checkpoint переключения")
    state = evidence["state"]
    validate_ready(state)
    if (state.get("run_id") != run_id or state.get("generation") != state.get("attempt")
            or ingress_config(state.get("intake_state", {})) != {**source, "phase": "active"}
            or sequence_cursor(state.get("intake_state", {}).get("serverless_sequence", 0)) != 0
            or state["snapshot"]["sha256"] != source.get("seed_snapshot_sha256")):
        raise ValueError("Checkpoint не подтверждает исходное переключение приёмника")
    return _timestamp(state.get("started_at"))


def _metadata(client: httpx.Client, url: str, bridge: str,
              activation_cutoff: datetime, expected_sha256: str) -> dict[str, Any]:
    result = _transfer(client, url, bridge, {"operation": "metadata"})
    if set(result) != {"status", "state", "config"} or result["status"] != "ready":
        raise ValueError("Нет подтверждённых метаданных приёмника")
    for name in ("state", "config"):
        item = result[name]
        if (not isinstance(item, dict) or set(item) != {"etag_sha256", "uploaded_at", "size"}
                or not isinstance(item["etag_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", item["etag_sha256"])
                or type(item["size"]) is not int or not 0 < item["size"] <= 8 * 1024 * 1024
                or _timestamp(item["uploaded_at"]) > activation_cutoff):
            raise ValueError("Приёмник изменялся после активации или метаданные неполны")
    if _timestamp(result["state"]["uploaded_at"]) != _timestamp(result["config"]["uploaded_at"]):
        raise ValueError("Состояние и настройки приёмника созданы не одним bootstrap")
    if result["state"]["etag_sha256"] != expected_sha256:
        raise ValueError("Состояние приёмника не совпадает с проверенным пустым снимком")
    return {"state": result["state"], "config": result["config"]}


def _audit(storage: SSHStateClient, work: Path, name: str, value: dict[str, Any], *,
           folder: str = "ingress-rollback-audit") -> str:
    content = canonical_json(value).encode()
    path = work / name
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    if storage.upload(path, folder) != digest:
        raise ValueError("Хранилище не подтвердило журнал отката")
    return digest


def _redis_storage_info(client: httpx.Client, url: str, bridge: str, *,
                        seed_sha256: str | None = None, allow_empty: bool) -> None:
    info = _transfer(client, url, bridge, {"operation": "storage-info"})
    base = {"status", "backend", "namespace", "initialized"}
    if (not base <= set(info) or info.get("status") != "ready"
            or info.get("backend") != "redis" or info.get("namespace") != "production"
            or type(info.get("initialized")) is not bool):
        raise ValueError("Не подтверждён отдельный рабочий Redis приёмника")
    if info["initialized"] is False:
        if not allow_empty or set(info) != base:
            raise ValueError("Redis приёмника не соответствует этапу миграции")
    elif (seed_sha256 is None or set(info) != base | {"seed_sha256", "mode"}
          or info.get("seed_sha256") != seed_sha256
          or info.get("mode") not in ({"active", "incomplete"} if allow_empty else {"active"})):
        raise ValueError("Redis уже содержит другое состояние приёмника")


def _redis_migration(state: dict[str, Any], url: str) -> dict[str, Any] | None:
    rollback = state.get("ingress_rollback")
    if (not isinstance(rollback, dict) or type(rollback.get("version")) is not int
            or rollback["version"] != 1 or rollback.get("operation") != "rollback-empty"
            or rollback.get("phase") != "complete" or rollback.get("url") != url
            or not isinstance(rollback.get("completion_audit_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", rollback["completion_audit_sha256"])):
        raise ValueError("Миграция Redis требует завершённого проверяемого отката")
    intake = state.get("intake_state", {})
    source = ingress_config(intake)
    if "ingress_migration" not in state:
        if source is not None or sequence_cursor(intake.get("serverless_sequence", 0)) != 0:
            raise ValueError("Новая миграция требует polling и нулевой позиции приёмника")
        return None
    marker = state["ingress_migration"]
    keys = {"version", "operation", "backend", "namespace", "phase", "url", "seed_sha256",
            "seed_snapshot_sha256", "rollback_audit_sha256", "created_at", "request_audit_sha256"}
    if (not isinstance(marker, dict) or type(marker.get("version")) is not int
            or marker["version"] != 1 or marker.get("operation") != "migrate-redis"
            or marker.get("backend") != "redis" or marker.get("namespace") != "production"
            or marker.get("phase") not in {"prepared", "active"} or marker.get("url") != url
            or set(marker) != keys | (
                {"activated_at"} if marker.get("phase") == "active" else set())
            or marker.get("rollback_audit_sha256") != rollback["completion_audit_sha256"]
            or any(not isinstance(marker.get(key), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", marker[key])
                   for key in ("seed_sha256", "seed_snapshot_sha256", "request_audit_sha256"))
            or source is None or source.get("url") != url
            or any(source.get(key) != marker.get(key) for key in (
                "phase", "seed_sha256", "seed_snapshot_sha256", "created_at"))
            or (marker["phase"] == "active"
                and source.get("activated_at") != marker.get("activated_at"))
            or (marker["phase"] == "prepared"
                and sequence_cursor(intake.get("serverless_sequence", 0)) != 0)):
        raise ValueError("Канонический маркер миграции Redis не совпадает с приёмником")
    _timestamp(marker["created_at"])
    if marker["phase"] == "active":
        _timestamp(marker["activated_at"])
    return marker


def rollback_empty(storage: SSHStateClient, url: str, work: Path, client: httpx.Client,
                   token: str, bridge_token: str, expected_state_sha256: str,
                   activation_checkpoint_sha256: str, activation_run_id: str, *,
                   now: datetime | None = None) -> dict[str, Any]:
    """Restore polling only for the independently verified, untouched bootstrap state."""
    if (url != PRODUCTION_ORIGIN
            or not re.fullmatch(r"[0-9a-f]{64}", expected_state_sha256)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", bridge_token)):
        raise ValueError("Для отката нужен рабочий приёмник и проверенный хеш пустого состояния")
    at = now or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("Время отката должно содержать часовой пояс")
    _phase("verify_rollback_bot")
    me = _api(client, token, "getMe", {})
    if (not isinstance(me, dict) or me.get("is_bot") is not True
            or me.get("username") != BOT_USERNAME or type(me.get("id")) is not int):
        raise ValueError("Токен не относится к рабочему редакционному боту")
    state, sha = storage.read()
    validate_ready(state)
    intake = state.get("intake_state", {})
    if sequence_cursor(intake.get("serverless_sequence", 0)) != 0:
        raise ValueError("Непустую очередь приёмника нельзя откатывать без переноса событий")
    source = ingress_config(intake)
    marker = state.get("ingress_rollback")
    if marker is not None:
        if (not isinstance(marker, dict) or marker.get("version") != 1
                or marker.get("operation") != "rollback-empty"
                or marker.get("phase") not in {"prepared", "complete"}
                or marker.get("url") != url
                or marker.get("expected_state_sha256") != expected_state_sha256
                or marker.get("activation_checkpoint_sha256") != activation_checkpoint_sha256
                or marker.get("activation_run_id") != activation_run_id):
            raise ValueError("Канонический маркер отката не совпадает с операцией")
        if marker["phase"] == "complete":
            if source is not None:
                raise ValueError("После завершённого отката снова настроен приёмник")
            webhook = _api(client, token, "getWebhookInfo", {})
            if not isinstance(webhook, dict) or webhook.get("url") != "":
                raise ValueError("После завершённого отката снова включён webhook")
            return {"status": "polling_restored", "already_complete": True}
    if (source is None or source["url"] != url
            or source["phase"] != ("prepared" if marker is not None else "active")):
        raise ValueError("Откат требует активного или подготовленного к откату приёмника")
    activation_cutoff = _activation_evidence(
        storage, activation_checkpoint_sha256, activation_run_id, source,
    )
    webhook = _api(client, token, "getWebhookInfo", {})
    destination = f"{url}/api/telegram"
    if (not isinstance(webhook, dict)
            or webhook.get("url") not in ({destination, ""} if marker else {destination})):
        raise ValueError("Webhook не соответствует проверяемому откату")
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    _phase("verify_untouched_ingress_metadata")
    before = _metadata(client, url, bridge_token, activation_cutoff, expected_state_sha256)
    if marker is None:
        request_record = {
            "version": 1, "operation": "rollback-empty", "phase": "prepared", "url": url,
            "expected_state_sha256": expected_state_sha256, "metadata": before,
            "activation_checkpoint_sha256": activation_checkpoint_sha256,
            "activation_run_id": activation_run_id,
            "seed_sha256": source["seed_sha256"], "activated_at": source["activated_at"],
            "requested_at": at.isoformat(), "snapshot_sha256": state["snapshot"]["sha256"],
        }
        request_sha = _audit(storage, work, "rollback-empty-request.json", request_record)
        marker = {**request_record, "request_audit_sha256": request_sha}
        _phase("fence_polling_for_rollback")
        state, sha = _commit_republication(storage, storage, state, sha, {
            "ingress_rollback": marker,
            "intake_state": {**intake, "ingress": {**source, "phase": "prepared"}},
        }, os.environ, work)
    elif (marker.get("metadata") != before or marker.get("seed_sha256") != source["seed_sha256"]
          or marker.get("activated_at") != source["activated_at"]
          or not isinstance(marker.get("request_audit_sha256"), str)
          or not re.fullmatch(r"[0-9a-f]{64}", marker["request_audit_sha256"])):
        raise ValueError("Метаданные приёмника изменились после подготовки отката")
    _phase("delete_webhook_preserving_pending_updates")
    if _api(client, token, "deleteWebhook", {"drop_pending_updates": False}) is not True:
        raise ValueError("Telegram не подтвердил выключение webhook")
    verified = _api(client, token, "getWebhookInfo", {})
    if not isinstance(verified, dict) or verified.get("url") != "":
        raise ValueError("Webhook всё ещё включён после запроса выключения")
    # An invocation already dispatched before deleteWebhook can still write.
    # Polling remains fenced for maxDuration plus the same teardown grace used by ingress.
    _phase("wait_for_dispatched_webhooks")
    time.sleep(ROLLBACK_QUIESCE_SECONDS)
    _phase("verify_unchanged_ingress_after_webhook_removal")
    after = _metadata(client, url, bridge_token, activation_cutoff, expected_state_sha256)
    if after != before:
        raise ValueError("Приёмник изменился во время выключения webhook")
    fresh, fresh_sha = storage.read()
    validate_ready(fresh)
    if (fresh, fresh_sha) != (state, sha):
        raise ValueError("Каноническое состояние изменилось во время отката")
    audit_sha = _audit(storage, work, "rollback-empty-complete.json", {
        **marker, "phase": "complete", "completed_at": at.isoformat(),
        "metadata_after": after, "webhook_url": "",
        "quiesce_seconds": ROLLBACK_QUIESCE_SECONDS,
    })
    restored_intake = dict(fresh["intake_state"])
    del restored_intake["ingress"]
    _phase("restore_canonical_polling")
    _commit_republication(storage, storage, fresh, fresh_sha, {
        "intake_state": restored_intake,
        "ingress_rollback": {**marker, "phase": "complete", "completed_at": at.isoformat(),
                             "completion_audit_sha256": audit_sha},
    }, os.environ, work)
    return {"status": "polling_restored", "state_sha256": expected_state_sha256,
            "audit_sha256": audit_sha}


def execute(storage: SSHStateClient, url: str, work: Path, client: httpx.Client,
            token: str, bridge_token: str, webhook_secret: str, *, activate: bool = False,
            migrate_redis: bool = False,
            now: datetime | None = None) -> dict[str, Any]:
    if url != PRODUCTION_ORIGIN:
        raise ValueError("Перенос разрешён только в отдельный проект приёмника")
    at = now or datetime.now(UTC)
    if at.tzinfo is None:
        raise ValueError("Время переноса должно содержать часовой пояс")
    if migrate_redis and not activate:
        raise ValueError("Миграция Redis является отдельной операцией переключения")
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
    rollback = state.get("ingress_rollback")
    migration = None
    if migrate_redis:
        migration = _redis_migration(state, url)
        if migration is None and current_webhook.get("url") != "":
            raise ValueError("Новая миграция Redis требует выключенного webhook")
        _phase("verify_redis_migration_storage")
        _redis_storage_info(
            client, url, bridge_token,
            seed_sha256=migration["seed_sha256"] if migration else None,
            allow_empty=migration is None or migration["phase"] == "prepared",
        )
    if activate and not migrate_redis and (rollback is not None
                                          or state.get("ingress_migration") is not None):
        raise ValueError("Повторное включение приёмника после отката требует отдельной миграции")
    if isinstance(rollback, dict) and rollback.get("phase") == "prepared":
        raise ValueError("Сначала требуется завершить начатый откат приёмника")
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    existing = ingress_config(state.get("intake_state", {}))
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
        updates: dict[str, Any] = {
            "intake_state": {**state.get("intake_state", {}), "ingress": metadata},
        }
        if migrate_redis:
            migration_record = {
                "version": 1, "operation": "migrate-redis", "backend": "redis",
                "namespace": "production", "phase": "prepared", "url": url,
                "seed_sha256": seed_sha, "seed_snapshot_sha256": state["snapshot"]["sha256"],
                "rollback_audit_sha256": cast(dict[str, Any], rollback)["completion_audit_sha256"],
                "created_at": at.isoformat(),
            }
            audit_sha = _audit(storage, work, "redis-migration-request.json", migration_record,
                               folder="ingress-migration-audit")
            migration = {**migration_record, "request_audit_sha256": audit_sha}
            updates["ingress_migration"] = migration
        state, state_sha = _commit_republication(
            storage, storage, state, state_sha, updates, os.environ, work,
        )
    if migrate_redis:
        _redis_storage_info(client, url, bridge_token, seed_sha256=seed_sha,
                            allow_empty=migration is not None and migration["phase"] == "prepared")
    _phase("bootstrap_private_ingress")
    result = _transfer(client, url, bridge_token, {
        "operation": "bootstrap", "seed_json": seed_bytes.decode(), "seed_sha256": seed_sha,
        "bot_token": token, "webhook_secret": webhook_secret, "mode": "active",
    })
    if (result.get("status") != "ready" or result.get("seed_sha256") != seed_sha
            or result.get("can_send") is not True):
        raise ValueError("Приёмник не подтвердил готовность рабочего режима")
    if migrate_redis:
        _redis_storage_info(client, url, bridge_token, seed_sha256=seed_sha, allow_empty=False)
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
    updates = {"intake_state": {
        **fresh["intake_state"], "ingress": {**fresh["intake_state"]["ingress"],
            "phase": "active", "activated_at": at.isoformat()},
    }}
    if migrate_redis:
        updates["ingress_migration"] = {**cast(dict[str, Any], migration), "phase": "active",
                                        "activated_at": at.isoformat()}
    _phase("activate_canonical_ingress")
    _commit_republication(storage, storage, fresh, fresh_sha, updates, os.environ, work)
    return {"status": "active", **summary, "origin": url,
            "pending_update_count": verified.get("pending_update_count", 0)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=("preflight", "activate", "rollback-empty",
                                               "migrate-redis"),
                        default="preflight")
    parser.add_argument("--expected-state-sha256", default="")
    parser.add_argument("--activation-checkpoint-sha256", default="")
    parser.add_argument("--activation-run-id", default="")
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
            if args.operation == "rollback-empty":
                result = rollback_empty(storage, args.url, args.work, client,
                                        os.environ["TELEGRAM_BOT_TOKEN"],
                                        os.environ.get("EDITORIAL_INGRESS_TOKEN", ""),
                                        args.expected_state_sha256,
                                        args.activation_checkpoint_sha256, args.activation_run_id)
            else:
                result = execute(storage, args.url, args.work, client,
                                 os.environ["TELEGRAM_BOT_TOKEN"],
                                 os.environ.get("EDITORIAL_INGRESS_TOKEN", ""),
                                 os.environ.get("EDITORIAL_INGRESS_WEBHOOK_SECRET", ""),
                                 activate=args.operation in {"activate", "migrate-redis"},
                                 migrate_redis=args.operation == "migrate-redis")
    except Exception as error:
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result))
    if result["status"] == "preflight_blocked_notices":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
