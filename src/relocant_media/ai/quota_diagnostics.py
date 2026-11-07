"""Bounded, content-free quota coordinates; unknown limits remain absent."""
import math
import re
from typing import Any


def safe_quota(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    delay = value.get("retry_seconds")
    if (isinstance(delay, (int, float)) and not isinstance(delay, bool)
            and math.isfinite(delay) and 0 < delay <= 86400):
        result["retry_seconds"] = delay
    limits = []
    for row in value.get("limits", [])[:5] if isinstance(value.get("limits"), list) else []:
        if not isinstance(row, dict):
            continue
        metric, limit, period = row.get("metric"), row.get("limit"), row.get("period")
        if (not isinstance(metric, str) or not re.fullmatch(r"[a-z_]{1,100}", metric)
                or not metric.startswith("generate_content_")
                or not isinstance(period, str) or period not in {"minute", "day", "unknown"}):
            continue
        entry: dict[str, Any] = {"metric": metric, "period": period}
        if type(limit) is int and 0 <= limit <= 10**12:
            entry["limit"] = limit
        limits.append(entry)
    if limits:
        result["limits"] = limits
    return result


def quota_details(payload: Any, retry_after: str = "") -> dict[str, Any]:
    summary: dict[str, Any] = {}
    if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", retry_after):
        summary["retry_seconds"] = float(retry_after)
    error = payload.get("error") if isinstance(payload, dict) else None
    details = error.get("details", []) if isinstance(error, dict) else []
    limits = []
    for detail in details[:10] if isinstance(details, list) else []:
        if not isinstance(detail, dict):
            continue
        if detail.get("@type") == "type.googleapis.com/google.rpc.RetryInfo":
            delay = detail.get("retryDelay")
            if isinstance(delay, str) and re.fullmatch(r"[0-9]+(?:\.[0-9]+)?s", delay):
                summary["retry_seconds"] = max(summary.get("retry_seconds", 0), float(delay[:-1]))
        if detail.get("@type") != "type.googleapis.com/google.rpc.QuotaFailure":
            continue
        violations = detail.get("violations", [])
        for row in violations[:5] if isinstance(violations, list) else []:
            if not isinstance(row, dict):
                continue
            metric, identifier = row.get("quotaMetric"), row.get("quotaId")
            limit = row.get("quotaValue")
            if (not isinstance(metric, str)
                    or not metric.startswith("generativelanguage.googleapis.com/")):
                continue
            period = "minute" if isinstance(identifier, str) and "PerMinute" in identifier else (
                "day" if isinstance(identifier, str) and "PerDay" in identifier else "unknown")
            entry: dict[str, Any] = {"metric": metric.split("/", 1)[1], "period": period}
            if isinstance(limit, str) and re.fullmatch(r"[0-9]{1,13}", limit):
                entry["limit"] = int(limit)
            elif type(limit) is int:
                entry["limit"] = limit
            limits.append(entry)
    summary["limits"] = limits
    return safe_quota(summary)
