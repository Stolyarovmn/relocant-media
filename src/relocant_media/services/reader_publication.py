"""Reader-selected permission and credit, bound to exact materials and a human review."""

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from importlib.resources import files
from typing import Any

from relocant_media.services.reader_forwarded_source import has_forwarded_parts, public_forward_urls

POLICY: dict[str, Any] = json.loads(
    files("relocant_media.domain").joinpath("reader_publication_v1.json").read_text("utf-8"),
)
TERMS_SHA256 = hashlib.sha256(POLICY["terms"].encode()).hexdigest()
LABELS: dict[str, str] = POLICY["labels"]


def begin_publication(payload: dict[str, Any], username: object) -> dict[str, Any]:
    result = deepcopy(payload)
    result["reader_publication"] = {
        "version": POLICY["version"], "stage": "ownership",
        "material_sha256": material_fingerprint(payload),
        "available_username": f"@{username}" if isinstance(username, str)
        and re.fullmatch(r"[A-Za-z0-9_]{5,32}", username) else None,
        "scope": {"channel_id": POLICY["channel_id"], "channel_url": POLICY["channel_url"]},
        "terms": POLICY["terms"], "terms_sha256": TERMS_SHA256,
    }
    return result


def publication_prompt(payload: dict[str, Any], reference: str) -> tuple[str, list[str]]:
    r, labels = payload["reader_publication"], LABELS
    stage = r["stage"]
    if stage == "ownership":
        return ("Чьи материалы вы прислали? Выбор относится ко всем сообщениям и вложениям. "
                "Если авторы разные, укажите это в тексте.",
                [labels["own"], labels["authorized"], labels["source"]])
    if stage == "permission":
        return POLICY["terms"], [labels["allow"], labels["private"]]
    if stage == "signature":
        return ("Как подписать материал? Имя и фото профиля автоматически не публикуются. "
                "Без подписи — без имени и ссылки в посте; контакт для ответа остаётся закрытым.",
                [labels["anonymous"],
                 *([labels["username"]] if r.get("available_username") else []), labels["name"]])
    if stage == "name":
        return ("Напишите имя или псевдоним до 40 знаков. Фамилия, почта и телефон не нужны. "
                "Это сообщение станет только подписью.", [labels["anonymous"], labels["back"]])
    if stage == "credit_confirm":
        return (f"В публикации будет показана подпись: {r['signature']['label']}\n\n"
                "Разрешаете открыто показать именно эту подпись читателям «Еду в Белград»? "
                + ("Подпись будет ссылкой на ваш Telegram-профиль. "
                   if r["signature"]["mode"] == "username"
                   else "Имя будет показано обычным текстом без ссылки на профиль. ")
                + "Другие сведения не добавляются.",
                [labels["credit"], labels["back"], labels["anonymous"]])
    if stage == "materials":
        return ("Дополните материалы, затем нажмите «Отправить». "
                "Новое содержимое нужно подтвердить заново.", [f"Отправить №{reference}"])
    return (
        f"Проверьте обращение №{reference}\n\n"
        f"Вложений: {len(payload.get('attachments', []))}.\n"
        f"Использование: {'разрешена публикация в «Еду в Белград»' if r['permission'] == 'allow'
                         else 'только для редакции'}.\n"
        f"Подпись: {r['signature']['label'] or 'без подписи'}.\n\n"
        "Материал проверит редактор. Согласие не означает автоматическую публикацию.",
        [f"Отправить №{reference}", *([labels["editCredit"]] if r["permission"] == "allow" else []),
         labels["editPermission"], labels["more"]],
    )


def advance_publication(
    payload: dict[str, Any], reference: str, message: dict[str, Any], at: datetime,
) -> tuple[dict[str, Any], bool] | None:
    """Canonical relay uses the same stages; no queue path may infer a grant."""
    if not isinstance(payload.get("reader_publication"), dict):
        return None
    result = deepcopy(payload)
    r, labels, text = result["reader_publication"], LABELS, message.get("text", "")
    stage = r["stage"]
    evidence = {"message_id": message["message_id"], "at": at.isoformat()}
    submit = text == f"Отправить №{reference}"
    next_stage: str | None = None
    if text in (f"Отменить №{reference}", f"Сменить тему №{reference}"):
        return None
    if stage == "materials" and submit:
        return begin_publication(payload, payload.get("reader_username")), False
    if stage == "ownership" and text in (labels["own"], labels["authorized"], labels["source"]):
        r["ownership"] = next(k for k in ("own", "authorized", "source") if text == labels[k])
        r["ownership_evidence"] = evidence
        next_stage = "permission"
        if r["ownership"] == "source":
            r.update(permission="private", permission_evidence=evidence,
                     signature={"mode": "anonymous", "label": "", "evidence": evidence})
            next_stage = "review"
    elif stage == "permission" and text in (labels["allow"], labels["private"]):
        r["permission"] = "allow" if text == labels["allow"] else "private"
        r["permission_evidence"] = evidence
        next_stage = "signature"
        if r["permission"] == "private":
            r["signature"] = {"mode": "anonymous", "label": "", "evidence": evidence}
            next_stage = "review"
    elif stage in ("signature", "name", "credit_confirm") and text == labels["anonymous"]:
        r["signature"] = {"mode": "anonymous", "label": "", "evidence": evidence}
        next_stage = "review"
    elif stage in ("name", "credit_confirm") and text == labels["back"]:
        next_stage = "signature"
    elif stage == "signature" and text == labels["username"] and r.get("available_username"):
        r["signature"] = {"mode": "username", "label": r["available_username"]}
        next_stage = "credit_confirm"
    elif stage == "signature" and text == labels["name"]:
        next_stage = "name"
    elif (stage == "name" and isinstance(text, str) and text not in labels.values()
          and not any(message.get(k) for k in ("photo", "video", "voice", "document", "audio"))):
        name = text.strip()
        if (name and len(name) <= 40 and not re.search(r"\d{5}", name)
                and all(c.isalnum() or c in " ._'’-" for c in name)):
            r["signature"] = {"mode": "name", "label": name}
            next_stage = "credit_confirm"
        else:
            return result, False
    elif stage == "credit_confirm" and text == labels["credit"]:
        r["signature"].update(evidence=evidence, disclosure={
            **evidence, "channel_id": POLICY["channel_id"], "label": r["signature"]["label"],
        })
        next_stage = "review"
    elif stage == "review" and text == labels["editCredit"] and r["permission"] == "allow":
        next_stage = "signature"
    elif stage == "review" and text == labels["editPermission"]:
        next_stage = "ownership"
    elif stage == "review" and text == labels["more"]:
        next_stage = "materials"
    elif stage == "review" and submit:
        if r["material_sha256"] != material_fingerprint(result):
            return begin_publication(payload, payload.get("reader_username")), False
        r.update(stage="confirmed", confirmed=evidence)
        return result, True
    elif submit or text in labels.values() or text.startswith("/"):
        return result, False
    else:
        return None
    r["stage"] = next_stage
    return result, False


def material_fingerprint(payload: dict[str, Any]) -> str:
    value: dict[str, Any] = {"text": payload.get("text", ""), "attachments": [
        {"kind": a["kind"], "file_id": a["file_id"], "file_unique_id": a.get("file_unique_id")}
        for a in payload.get("attachments", [])
    ]}
    if urls := public_forward_urls(payload):
        value["public_forward_urls"] = urls
    if has_forwarded_parts(payload):
        value["forwarded"] = True
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
    """The only reusable reader-material projection: never include private identity fields.

    Files without the owner's exact content approval are left out (owner 23.11.2026):
    they no longer block the reader's permitted text, and a file never ships unreviewed.
    """
    reason = permission_reason(payload, target_channel_id)
    if reason:
        raise ValueError(reason)
    if not str(payload.get("text", "")).strip():
        if reason := human_review_reason(payload, owner_user_id):
            raise ValueError(reason)
    attachments = ([] if human_review_reason(payload, owner_user_id)
                   else payload.get("attachments", []))
    return {"text": payload["text"], "attachments": [
        {**attachment, "credit": reader_credit(payload, kind=attachment["kind"])}
        for attachment in attachments
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
