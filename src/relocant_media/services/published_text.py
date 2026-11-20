"""Read-only проверка отображаемой редакции; исходный пакет остаётся историей.

Не импортирует AI/runtime/Telegram и не создаёт допуска публикации.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from relocant_media.domain.channel_targets import APPROVED_CHANNELS_BY_CITY
from relocant_media.storage.models import EditorialRun, Post


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _digest(value: Any) -> str:
    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("Повтор поля в плане исправления")
        result[key] = value
    return result


@dataclass(frozen=True)
class PublishedText:
    title: str
    body: str
    corrected: bool = False


def effective_published_text(post: Post, run: EditorialRun) -> PublishedText:
    original = PublishedText(post.title, post.body)
    overlay = run.outcome.get("_published_correction")
    if overlay is None:
        return original
    try:
        if (not isinstance(overlay, dict) or overlay.get("version") != 4
                or overlay.get("status") != "corrected" or run.delivery_status != "sent"
                or run.post_id != post.id or post.telegram_message_id is None
                or post.published_at is None or run.staging_message_id is not None):
            return original
        raw_plan = overlay["plan_json"]
        if not isinstance(raw_plan, str) or len(raw_plan.encode()) > 32 * 1024:
            return original
        plan = json.loads(raw_plan, object_pairs_hook=unique_object)
        intent, receipt = overlay["intent"], overlay["receipt"]
        channel = APPROVED_CHANNELS_BY_CITY.get(post.city)
        if (channel is None or not isinstance(intent, dict) or not isinstance(receipt, dict)
                or any(type(value) is not int for value in (
                    overlay["version"], plan["version"], intent["version"], receipt["version"],
                    plan["raw_item_id"], plan["post_id"], plan["message_id"],
                    intent["chat_id"], intent["message_id"],
                    receipt["chat_id"], receipt["message_id"]))
                or intent["version"] != 4 or receipt["version"] != 4
                or intent["operation"] != "editMessageText"
                or receipt["operation"] != "editMessageText"
                or intent["chat_id"] != channel or receipt["chat_id"] != channel
                or any(type(receipt.get(key)) is not type(value) or receipt.get(key) != value
                       for key, value in intent.items())):
            return original
        reconciliation_fields = {"reconciliation_kind", "previous_receipt_sha256",
            "previous_receipt", "reconciled_at", "reconciliation_checkpoint_sha256",
            "reconciliation_state_sha256", "reconciliation_snapshot_sha256"}
        if reconciliation_fields.intersection(receipt):
            previous = receipt.get("previous_receipt")
            if (not reconciliation_fields.issubset(receipt)
                    or receipt.get("reconciliation_kind") != "late_public_readback"
                    or not isinstance(previous, dict)
                    or receipt["previous_receipt_sha256"] != _digest(previous)
                    or previous.get("api_confirmed") is not True
                    or previous.get("public_confirmed") is not False
                    or previous.get("status") != "uncertain"
                    or previous.get("intent_sha256") != overlay["intent_sha256"]
                    or reconciliation_fields.intersection(previous) or "error_type" in previous
                    or not isinstance(receipt.get("reconciled_at"), str)
                    or any(not isinstance(receipt[field], str)
                           or re.fullmatch(r"[0-9a-f]{64}", receipt[field]) is None for field in (
                               "reconciliation_checkpoint_sha256", "reconciliation_state_sha256",
                               "reconciliation_snapshot_sha256"))
                    or any(type(previous.get(key)) is not type(value)
                           or previous.get(key) != value for key, value in intent.items())):
                return original
        package = run.outcome["_package"]
        expected_package = run.outcome["_package_sha256"]
        for key in ("plan_sha256", "intent_sha256", "receipt_sha256"):
            if not re.fullmatch(r"[0-9a-f]{64}", overlay[key]):
                return original
        old_text = f"{post.title}\n\n{post.body}\n\nИсточник"
        new_text = f"{plan['title']}\n\n{plan['body']}\n\nИсточник"
        if (overlay["plan_sha256"] != _sha(raw_plan)
                or overlay["intent_sha256"] != _digest(intent)
                or overlay["receipt_sha256"] != _digest(receipt)
                or plan["version"] != 4 or plan["operation"] != "remove_unsupported_claims"
                or plan["raw_item_id"] != run.raw_item_id or plan["post_id"] != post.id
                or plan["message_id"] != post.telegram_message_id or plan["city"] != post.city
                or plan["old_title_sha256"] != _sha(post.title)
                or plan["old_body_sha256"] != _sha(post.body)
                or plan["old_text_sha256"] != _sha(old_text)
                or plan["old_package_sha256"] != expected_package
                or _digest(package) != expected_package
                or package["raw_item_id"] != run.raw_item_id or package["city"] != post.city
                or package["outcome"]["draft"]["title"] != post.title
                or package["outcome"]["draft"]["body"] != post.body
                or package["presentation"]["illustration"]["source_url"] != plan["source_url"]
                or intent["plan_sha256"] != overlay["plan_sha256"]
                or intent["old_package_sha256"] != expected_package
                or intent["old_text_sha256"] != _sha(old_text)
                or intent["new_text_sha256"] != _sha(new_text)
                or receipt["intent_sha256"] != overlay["intent_sha256"]
                or receipt["plan_sha256"] != overlay["plan_sha256"]
                or receipt["message_id"] != post.telegram_message_id
                or intent["message_id"] != post.telegram_message_id
                or receipt["chat_id"] != intent["chat_id"]
                or receipt["public_text_sha256"] != _sha(new_text)
                or receipt["status"] != "corrected" or receipt["api_confirmed"] is not True
                or receipt["public_confirmed"] is not True):
            return original
        reviews = plan["reviews"]
        context = _digest({key: value for key, value in plan.items() if key != "reviews"})
        if (len(reviews) != 2 or {review["role"] for review in reviews} != {"facts", "utility"}
                or len({review["reviewer_id"].strip().casefold() for review in reviews}) != 2
                or any(review["context_sha256"] != context or review["verdict"] != "approve"
                       or review["score"] < 75 or review["flags"]
                       or review["live_source_checked"] is not True
                       or review["scope"] != "remove_unsupported_claims_only"
                       or review["factual_support"] != "supported"
                       or review["no_new_sensitive_claims"] is not True for review in reviews)):
            return original
        return PublishedText(plan["title"], plan["body"], corrected=True)
    except (KeyError, TypeError, ValueError, AttributeError, RecursionError, OverflowError):
        return original
