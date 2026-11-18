"""Reader-selected permission and credit, bound to exact materials and a human review."""

import hashlib
import json
import re
from datetime import datetime
from importlib.resources import files
from typing import Any

POLICY: dict[str, Any] = json.loads(
    files("relocant_media.domain").joinpath("reader_publication_v1.json").read_text("utf-8"),
)
TERMS_SHA256 = hashlib.sha256(POLICY["terms"].encode()).hexdigest()


def material_fingerprint(payload: dict[str, Any]) -> str:
    value = {"text": payload.get("text", ""), "attachments": [
        {"kind": a["kind"], "file_id": a["file_id"], "file_unique_id": a.get("file_unique_id")}
        for a in payload.get("attachments", [])
    ]}
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, separators=(",", ":"),
    ).encode()).hexdigest()


def _evidence(value: Any) -> bool:
    if (not isinstance(value, dict) or type(value.get("message_id")) is not int
            or value["message_id"] <= 0 or not isinstance(value.get("at"), str)):
        return False
    try:
        return datetime.fromisoformat(value["at"].replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def permission_reason(payload: dict[str, Any], target_channel_id: int) -> str | None:
    r = payload.get("reader_publication")
    if not isinstance(r, dict):
        return "Нет подтверждённого разрешения автора."
    if (r.get("version") != POLICY["version"] or r.get("terms") != POLICY["terms"]
            or r.get("terms_sha256") != TERMS_SHA256
            or r.get("scope") != {"channel_id": target_channel_id,
                                  "channel_url": POLICY["channel_url"]}
            or target_channel_id != POLICY["channel_id"]):
        return "Разрешение не относится к этой версии условий или каналу."
    if r.get("permission") != "allow" or r.get("ownership") not in {"own", "authorized"}:
        return "Материалы переданы только редакции; публикация не разрешена."
    if (r.get("stage") != "confirmed" or not _evidence(r.get("confirmed"))
            or not _evidence(r.get("ownership_evidence"))
            or not _evidence(r.get("permission_evidence"))
            or r.get("material_sha256") != material_fingerprint(payload)):
        return "Содержимое изменилось или разрешение ещё не подтверждено."
    signature = r.get("signature", {})
    if not isinstance(signature, dict) or not _evidence(signature.get("evidence")):
        return "Не подтверждён выбор подписи."
    mode, label = signature.get("mode"), signature.get("label")
    if mode == "anonymous":
        return None if label == "" else "Анонимная публикация не должна раскрывать подпись."
    disclosure = signature.get("disclosure")
    if (mode not in {"username", "name"} or not isinstance(label, str) or not label
            or len(label) > 40 or not isinstance(disclosure, dict) or not _evidence(disclosure)
            or disclosure.get("label") != label
            or disclosure.get("channel_id") != target_channel_id):
        return "Нет отдельного подтверждения публичной подписи."
    if mode == "username" and not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", label):
        return "Недопустимый Telegram-псевдоним."
    if mode == "name" and (
        any(not (c.isalnum() or c in " ._'’-" or c.isalpha()) for c in label)
        or re.search(r"\d{5}", label)
    ):
        return "Недопустимое имя или псевдоним."
    return None


def human_review_reason(payload: dict[str, Any], owner_user_id: int | None) -> str | None:
    if not payload.get("attachments"):
        return None
    review = payload.get("reader_media_review", {})
    if (isinstance(review, dict) and review.get("decision") == "rejected"
            and review.get("material_sha256") == material_fingerprint(payload)):
        return "Вложения отклонены редактором; использовать их нельзя."
    if (type(owner_user_id) is not int or owner_user_id <= 0
            or not isinstance(review, dict) or review.get("decision") != "approved"
            or review.get("owner_user_id") != owner_user_id
            or review.get("material_sha256") != material_fingerprint(payload)
            or not isinstance(review.get("at"), str)):
        return "Вложения ожидают ручной проверки содержания человеком."
    return None


def reader_credit(payload: dict[str, Any], *, kind: str | None = None) -> str:
    if permission_reason(payload, POLICY["channel_id"]):
        return ""
    r = payload["reader_publication"]
    signature = r["signature"]
    if signature["mode"] == "anonymous":
        return ""
    label = {"photo": "Фото", "video": "Видео", "voice": "Аудио", "audio": "Аудио"}.get(
        kind or "", "Материал",
    )
    if r["ownership"] != "own":
        label = "Прислал"
    return f"{label}: {signature['label']}"


def public_reader_material(
    payload: dict[str, Any], *, target_channel_id: int, owner_user_id: int | None,
) -> dict[str, Any]:
    """The only reusable reader-material projection: never include private identity fields."""
    reason = permission_reason(payload, target_channel_id) or human_review_reason(
        payload, owner_user_id,
    )
    if reason:
        raise ValueError(reason)
    return {"text": payload["text"], "attachments": [
        {**attachment, "credit": reader_credit(payload, kind=attachment["kind"])}
        for attachment in payload.get("attachments", [])
    ], "credit": reader_credit(payload), "material_sha256": material_fingerprint(payload)}


def owner_publication_summary(payload: dict[str, Any]) -> str:
    r = payload.get("reader_publication")
    if not isinstance(r, dict):
        return ""
    reason = permission_reason(payload, POLICY["channel_id"])
    signature = r.get("signature", {})
    credit = signature.get("label") if isinstance(signature, dict) else None
    return (
        "\nРазрешение: " + (reason or "публикация в «Еду в Белград» разрешена.") +
        "\nПодпись: " + (str(credit) if credit and not reason else "не публикуется") +
        "\nФото и видео: обязательный ручной контроль содержания. Просмотрите все файлы: "
        "обнажённые люди, жестокие сцены и другие недопустимые материалы не выпускаются.\n"
    )
