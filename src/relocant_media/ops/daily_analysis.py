"""Read-only daily operational status from the public newsroom projection."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from relocant_media.ops.newsroom_export import CITIES
from relocant_media.ops.newsroom_export import snapshot as public_catalog
from relocant_media.ops.newsroom_merge import merge
from relocant_media.services.daily_brief import public_daily_brief
from relocant_media.services.editorial_flow import public_editorial_flow
from relocant_media.services.kpi import public_kpi

ZONE = ZoneInfo("Europe/Moscow")
STATES = (
    "sent", "ready", "review", "held", "failed", "uncertain", "sending",
    "needs_reverify", "expired", "evidence_ready", "discovered", "drafting",
)
_CHANNELS = {"belgrade": "https://t.me/eduvbelgrad"}
_CITY_ZONE = ZoneInfo("Europe/Belgrade")
_EDITIONS = {"morning": "Белград сегодня", "evening": "Главное за день"}
_EDITION_STATUS = {"pending": "ожидается", "unconfirmed": "выход не подтверждён",
                   "not_scheduled": "вне расписания"}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _delta(value: Any) -> int | None:
    return value if type(value) is int else None


def _rolling_kpi(recorded: dict[str, Any]) -> dict[str, Any]:
    """Скользящие окна проекции; не календарные сутки и не выводятся из sent."""
    kpi = public_kpi(recorded.get("kpi")) or {}
    published = kpi.get("published") or {}
    audience = kpi.get("subscribers") or {}
    last = _time(published.get("last_published_at"))
    measured = _time(audience.get("observed_at"))
    reference = _time(recorded.get("observed_at"))

    def change(key: str, window: timedelta) -> int | None:
        # A reading at or before the window start can be its own baseline: "+0" by one reading.
        if measured is None or reference is None or measured <= reference - window:
            return None
        return _delta(audience.get(key))

    return {
        "window": "rolling", "observed_at": recorded.get("observed_at"),
        "published_last_24h": _count(published.get("last_24h")),
        "published_last_7d": _count(published.get("last_7d")),
        "last_published_at": last.isoformat() if last else None,
        "subscribers": _count(audience.get("count")),
        "subscribers_observed_at": measured.isoformat() if measured else None,
        "subscribers_delta_24h": change("delta_24h", timedelta(hours=24)),
        "subscribers_delta_7d": change("delta_7d", timedelta(days=7)),
    }


def build_status(
    snapshot: dict[str, Any] | None, *, city: str, now: datetime, day: date | None = None,
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
    sources = {row["id"] for row in recorded.get("sources", [])
               if isinstance(row, dict) and isinstance(row.get("id"), str)}
    flow = public_editorial_flow(recorded.get("editorial_flow"), sources) if available else None
    brief = public_daily_brief(recorded.get("daily_brief")) if available else None
    zone = ZoneInfo(brief["timezone"]) if brief else _CITY_ZONE
    local_date = (day or now.astimezone(zone).date()).isoformat()
    day_snapshot = next((value for value in (brief or {}).get("days", [])
                         if value["local_date"] == local_date), None)
    if observed is not None and observed > now:
        day_snapshot = None
    cycle = recorded.get("last_cycle") if available else None
    cycle = cycle if isinstance(cycle, dict) else {}
    cycle_time = _time(cycle.get("observed_at"))
    same_day = (
        cycle_time is not None and cycle_time <= now
        and cycle_time.astimezone(zone).date().isoformat() == local_date
    )
    attempt = (snapshot or {}).get("last_attempt")
    attempt = attempt if isinstance(attempt, dict) else {}
    attempted = _time(attempt.get("observed_at"))
    workflow_failed = (
        city == "belgrade" and attempt.get("mode") in {"editorial", "intake", "fallback", "digest"}
        and attempt.get("status") == "failure" and attempted is not None and attempted <= now
        and attempted.astimezone(zone).date().isoformat() == local_date
    )
    cycle_health = {
        "errors": _count(cycle.get("processing_error_count")) if same_day else None,
        "fallback_needed": same_day and cycle.get("fallback_required") is True,
        "workflow_failed": workflow_failed,
    }
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
        "rolling_kpi": _rolling_kpi(recorded) if available else None,
        "daily_results": {"published_posts": None, "promotion": None,
                          "audience_metrics": None, "comment_signals": None,
                          "incoming_submissions": None},
        "owner_brief": {"local_date": local_date, "timezone": zone.key,
                        "publications": day_snapshot, "cycle": cycle_health},
        "quality": {"scope": "returned_snapshot_items_only",
                    "repeated_reason_codes": [
                        {"reason_code": key, "occurrences": value}
                        for key, value in reasons.most_common(5) if value > 1],
                    "trend_confirmed": False},
        "editorial_flow": flow,
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


def _shown(value: int | None) -> str:
    return "нет данных" if value is None else str(value)


def _signed(value: int | None) -> str:
    return "нет данных" if value is None else f"{value:+d}"


def _kpi_lines(kpi: dict[str, Any] | None) -> list[str]:
    if kpi is None:
        return ["Отдельные публикации и подписчики: нет данных."]
    observed = _time(kpi["observed_at"])
    stamp = observed.astimezone(ZONE).strftime("%d.%m %H:%M МСК") if observed else "неизвестно"
    lines = [f"Отдельные публикации за скользящие 24 ч до {stamp}: "
             f"{_shown(kpi['published_last_24h'])}; за 7 дней: "
             f"{_shown(kpi['published_last_7d'])}."]
    measured = _time(kpi["subscribers_observed_at"])
    when = measured.astimezone(ZONE).strftime("%d.%m %H:%M МСК") if measured else "время неизвестно"
    lines.append(f"Подписчики: {_shown(kpi['subscribers'])} ({when}); "
                 f"за 24 ч {_signed(kpi['subscribers_delta_24h'])}, "
                 f"за 7 дней {_signed(kpi['subscribers_delta_7d'])}.")
    return lines


def owner_markdown(report: dict[str, Any]) -> str:
    """A short read-only update with evidence scopes and unknowns made explicit."""
    brief = report["owner_brief"]
    zone = ZoneInfo(brief["timezone"])
    observed = _time(report["snapshot"]["observed_at"])
    observed_label = observed.astimezone(zone).strftime("%d.%m %H:%M") if observed else "нет"
    local_date = datetime.fromisoformat(brief["local_date"]).strftime("%d.%m.%Y")
    city = report["city"]
    lines = [f"**{CITIES.get(city, city)} · {local_date}**"]
    if report["snapshot"]["status"] == "stale":
        lines.append(f"Снимок устарел. Последние данные на {observed_label} по местному времени.")
    else:
        lines.append(f"Снимок на {observed_label} по местному времени." if observed
                     else "Рабочий снимок недоступен.")
    day = brief["publications"] or {}
    channel = _CHANNELS.get(city)

    def post_link(message_id: int, label: str) -> str:
        return f"[{label}]({channel}/{message_id})" if channel else label

    message_ids = day.get("message_ids", ())
    links = ", ".join(post_link(number, f"№{number}") for number in message_ids)
    if links and day["post_count"] > len(message_ids):
        links = f"Последние — {links}"
    publications = f"Отдельных постов за день — {_shown(day.get('post_count'))}."
    lines.append(f"{publications} {links}" if links else publications)
    editions = []
    for kind, title in _EDITIONS.items():
        edition = day.get(kind) or {}
        if edition.get("status") == "sent":
            editions.append(post_link(edition["message_id"], f"«{title}»"))
        else:
            status = _EDITION_STATUS.get(str(edition.get("status")), "нет данных")
            editions.append(f"«{title}» — {status}")
    lines.append("Сводки · " + " · ".join(editions) + ".")
    kpi = report["rolling_kpi"] or {}
    measured = _time(kpi.get("subscribers_observed_at"))
    label = measured.astimezone(zone).strftime("%d.%m %H:%M") if measured else "время неизвестно"
    coverage = (f"{day['views']} по {day['measured_posts']} из {day['post_count']} постов"
                if day.get("views") is not None else "нет данных")
    subscribers = _shown(kpi.get("subscribers"))
    lines.append(f"Подписчики — {subscribers} ({label}). Просмотры — {coverage}.")
    health = brief["cycle"]
    health_text = _shown(health["errors"])
    failure = " Последний запуск завершился ошибкой." if health["workflow_failed"] else ""
    fallback = " Цикл запросил резервную проверку." if health["fallback_needed"] else ""
    lines.append(f"Ошибки обработки в последнем цикле дня — {health_text}.{failure}{fallback}")
    counts = report["snapshot"]["queue_counts"] or {}
    uncertain = counts.get("uncertain", 0)
    lines.append(f"Сейчас нужно сверить с Telegram отправки с неизвестным исходом — {uncertain}."
                 if uncertain
                 else "Отдельные решения владельца в снимке не отражены.")
    return "  \n".join(lines) + "\n"


def markdown(report: dict[str, Any]) -> str:
    observed = _time(report["snapshot"]["observed_at"])
    stamp = observed.astimezone(ZONE).strftime("%d.%m.%Y %H:%M МСК") if observed else "неизвестно"
    lines = [f"# Ежедневный статус · {report['report_date']} · {report['city']}", "",
             f"Состояние снимка: {report['snapshot']['status']}. Наблюдение БД: {stamp}.", "",
             *_kpi_lines(report.get("rolling_kpi")),
             "Публикации за календарные сутки: неизвестно. Продвижение: недоступно.",
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
    flow = report.get("editorial_flow")
    lines.extend(["", "## Очередь и скорость выпуска", ""])
    if flow is None:
        lines.append("Для этого снимка агрегаты редакции недоступны.")
    else:
        zone = ZoneInfo(flow["timezone"])
        measured_at = datetime.fromisoformat(flow["observed_at"]).astimezone(zone)
        lines.append(f"Срез на {measured_at:%d.%m.%Y %H:%M} ({flow['timezone']}).")
        lines.extend(["", "| День | Истекло до полного разбора | Источники и причины |",
                      "| --- | ---: | --- |"])
        for day in flow["days"]:
            expired = day["expired_unreviewed"]
            breakdown = ", ".join(
                f"{row['source_id']} — {row['count']} ({row['reason']})" for row in expired
            ) or "—"
            date_label = day["local_date"]
            if date_label == measured_at.date().isoformat():
                date_label += " (неполный)"
            lines.append(f"| {date_label} | {sum(row['count'] for row in expired)} | {breakdown} |")
        lines.extend(["", "| День | Рубрика | Медиана, мин | С точным временем | Без времени |",
                      "| --- | --- | ---: | ---: | ---: |"])
        for day in flow["days"]:
            for latency in day["delivery_latency"]:
                minutes = latency["median_minutes"]
                label = "—" if minutes is None else str(minutes)
                lines.append(
                    f"| {day['local_date']} | {latency['category']} | {label} | "
                    f"{latency['samples']} | {latency['missing_timestamp']} |"
                )
        if not any(day["delivery_latency"] for day in flow["days"]):
            lines.append("| — | — | — | — | — |")
        stage_names = ("collection", "before_post_record", "after_post_record", "since_ingestion")
        stage_rows = [
            (day["local_date"], latency["category"], latency["stages"])
            for day in flow["days"] for latency in day["delivery_latency"] if latency.get("stages")
        ]
        if stage_rows:
            lines.extend([
                "", "Медианы по этапам в минутах. В скобках число замеров.",
                "", "| День | Рубрика | Источник → сбор | Сбор → запись поста | "
                "Запись поста → публикация | Сбор → публикация |",
                "| --- | --- | ---: | ---: | ---: | ---: |",
            ])
            for day, category, stages in stage_rows:
                values = [
                    "—" if stages[key]["median_minutes"] is None
                    else f"{stages[key]['median_minutes']} ({stages[key]['samples']})"
                    for key in stage_names
                ]
                lines.append(f"| {day} | {category} | " + " | ".join(values) + " |")
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
    parser.add_argument("--date", type=date.fromisoformat, help="local date of the brief")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # A missing data branch or snapshot is an unavailable observation, never an empty queue.
    snapshot = None
    if not args.snapshot_unavailable and args.snapshot.is_file():
        snapshot = merge(public_catalog(args.root), json.loads(args.snapshot.read_text()))
    report = build_status(snapshot, city=args.city, now=datetime.now(UTC), day=args.date)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "status.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.output / "status.md").write_text(markdown(report), encoding="utf-8")
    (args.output / "owner.md").write_text(owner_markdown(report), encoding="utf-8")


if __name__ == "__main__":
    main()
