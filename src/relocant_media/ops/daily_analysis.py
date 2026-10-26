"""Read-only daily operational status from the public newsroom projection."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from relocant_media.ops.newsroom_export import snapshot as public_catalog
from relocant_media.ops.newsroom_merge import merge

ZONE = ZoneInfo("Europe/Moscow")
STATES = (
    "sent", "ready", "review", "held", "failed", "uncertain", "sending",
    "needs_reverify", "expired", "evidence_ready", "discovered", "drafting",
)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def build_status(
    snapshot: dict[str, Any] | None, *, city: str, now: datetime,
) -> dict[str, Any]:
    """Never infer daily deliveries from source publication dates or lifetime totals."""
    if now.tzinfo is None:
        raise ValueError("timezone-aware report time required")
    if snapshot is not None and (
        snapshot.get("schema_version") != 1 or snapshot.get("mode") != "public_read_only"
        or not isinstance(snapshot.get("cities"), list)
    ):
        raise ValueError("public newsroom snapshot required")
    recorded = next((row for row in (snapshot or {}).get("cities", [])
                     if isinstance(row, dict) and row.get("id") == city), {})
    observed = _time(recorded.get("observed_at"))
    available = recorded.get("runtime_status") == "available" and observed is not None
    stale = observed is not None and available and (
        now - observed > timedelta(hours=6) or observed > now)
    counts = recorded.get("counts") if available else None
    counts = counts if isinstance(counts, dict) else {}
    safe_counts = {key: counts[key] for key in STATES
                   if type(counts.get(key)) is int and counts[key] >= 0}
    reasons: Counter[str] = Counter()
    items = recorded.get("items")
    if available and isinstance(items, list):
        for item in items:
            if not isinstance(item, dict) or item.get("state") == "sent":
                continue
            reason = item.get("reason_code")
            if isinstance(reason, str) and re.fullmatch(r"[a-z][a-z0-9_:-]{0,159}", reason):
                reasons[reason] += 1
    actions = []
    if not available:
        actions.append("Восстановить наблюдение канонической очереди; "
                       "пустой снимок не означает отсутствие новостей.")
    elif stale:
        actions.append("Проверить последний редакционный запуск и обновление снимка: "
                       "данные устарели.")
    if any(safe_counts.get(key, 0) for key in ("uncertain", "sending")):
        actions.append("Сверить неопределённые отправки с Telegram; не повторять их автоматически.")
    if safe_counts.get("needs_reverify", 0):
        actions.append("Разобрать изменившиеся первоисточники через штатную повторную проверку.")
    if any(value > 1 for value in reasons.values()):
        actions.append("Изучить повторяющиеся причины удержания в закрытом аудите, "
                       "сохраняя facts/utility guards.")
    actions.extend([
        "Подключить подтверждённые метрики публикаций и продвижения "
        "для оценки результата за сутки.",
        "Проверить доступность агрегированных сигналов комментариев и входящих обращений "
        "без выгрузки личных данных.",
        "Сверить фактический охват источников и стабильность Actions + Drive "
        "перед расширением автоматизации.",
    ])
    return {
        "schema_version": 1, "mode": "public_read_only", "city": city,
        "report_date": now.astimezone(ZONE).date().isoformat(), "timezone": "Europe/Moscow",
        "generated_at": now.astimezone(UTC).isoformat(),
        "snapshot": {
            "status": "unavailable" if not available else "stale" if stale else "available",
                     "observed_at": observed.isoformat() if observed else None,
                     "queue_counts": safe_counts if available else None},
        "daily_results": {"published_posts": None, "promotion": None,
                          "audience_metrics": None, "comment_signals": None,
                          "incoming_submissions": None},
        "quality": {"scope": "returned_snapshot_items_only",
                    "repeated_reason_codes": [
                        {"reason_code": key, "occurrences": value}
                        for key, value in reasons.most_common(5) if value > 1],
                    "trend_confirmed": False},
        "next_actions": actions[:5],
        "limitations": [
            "Количество sent относится к снимку очереди, а не к публикациям за сутки.",
            "Дата первоисточника не является датой отправки в Telegram.",
            "Метрики продвижения, комментарии и входящие обращения в публичном снимке недоступны.",
            "Повторяющиеся причины в показанной выборке не доказывают временной тренд "
            "или качество всех материалов.",
            "Отчёт не меняет очередь, публикации, настройки и рекламный бюджет.",
        ],
    }


def markdown(report: dict[str, Any]) -> str:
    observed = _time(report["snapshot"]["observed_at"])
    stamp = observed.astimezone(ZONE).strftime("%d.%m.%Y %H:%M МСК") if observed else "неизвестно"
    lines = [f"# Ежедневный статус · {report['report_date']} · {report['city']}", "",
             f"Состояние снимка: {report['snapshot']['status']}. Наблюдение БД: {stamp}.", "",
             "Публикации за сутки: неизвестно. Продвижение и метрики аудитории: недоступны.",
             "Сигналы комментариев и входящих обращений: недоступны.", ""]
    counts = report["snapshot"]["queue_counts"]
    if counts is not None:
        lines.extend(["Состояния очереди в снимке (не суточные результаты):", "",
                      ", ".join(f"{key}: {value}" for key, value in counts.items()) or "неизвестно",
                      ""])
    repeated = report["quality"]["repeated_reason_codes"]
    lines.extend(["Причины, повторяющиеся в показанной выборке:", ""])
    lines.extend([f"- {row['reason_code']}: {row['occurrences']}" for row in repeated]
                 or ["Недостаточно наблюдений; временной тренд не установлен."])
    lines.extend(["", "Следующие действия:", ""])
    lines.extend(f"{index}. {action}" for index, action in enumerate(report["next_actions"], 1))
    lines.extend(["", "Ограничения:", ""])
    lines.extend(f"- {value}" for value in report["limitations"])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--snapshot-unavailable", action="store_true",
                        help="ignore all snapshot bytes after a failed data fetch")
    parser.add_argument("--city", choices=("belgrade", "novi_sad"), default="belgrade")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # A missing data branch or snapshot is an unavailable observation, never an empty queue.
    snapshot = None
    if not args.snapshot_unavailable and args.snapshot.is_file():
        snapshot = merge(public_catalog(args.root), json.loads(args.snapshot.read_text()))
    report = build_status(snapshot, city=args.city, now=datetime.now(UTC))
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "status.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "status.md").write_text(markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
