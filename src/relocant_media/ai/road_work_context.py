"""Source-bound calendar context for explicit overnight road work."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from relocant_media.ai.models import EvidenceDocument
from relocant_media.domain.city_profile import CityEditorialProfile
from relocant_media.domain.road_work_time import overnight_road_schedule, road_work_clauses

ROAD_WORK_CONTEXT_INSTRUCTION = (
    " Сверь source_road_windows с исходной цитатой, где указан напечатанный интервал "
    "source_dates. Для каждого направления проверь свои даты. Общие часы не означают "
    "одинаковые даты для разных направлений или участков. Ночную смену указывай с датой "
    "начала и датой окончания. В практической части оставляй текущие и предстоящие смены; "
    "прошедшую смену не описывай как действующую. Если ещё есть текущая или предстоящая "
    "смена этого объявления, не считай его истекшим из-за завершения другой смены. "
    "Истекшие смены не дают основания для выпуска как текущей новости. "
    "Подсказка вычислена из исходной цитаты и не заменяет проверенные claims и evidence. "
)


def road_work_context(
    documents: tuple[EvidenceDocument, ...],
    now: datetime,
    profile: CityEditorialProfile | None,
) -> list[dict[str, object]]:
    zone = ZoneInfo(profile.timezone if profile else "Europe/Belgrade")
    now = now.astimezone(UTC)
    result: list[dict[str, object]] = []
    for document in documents:
        published = document.published_at
        if published is None or published.tzinfo is None:
            continue
        local_published = published.astimezone(zone)
        for clause in road_work_clauses(f"{document.title}\n{document.body}"):
            schedule = overnight_road_schedule(clause, local_published)
            if not schedule:
                continue
            windows = []
            for start, end, source_dates in schedule:
                status = "active"
                if end.astimezone(UTC) <= now:
                    status = "ended"
                elif now < start.astimezone(UTC):
                    status = "upcoming"
                windows.append({
                    "source_dates": source_dates,
                    "starts_at": start.isoformat(),
                    "ends_at": end.isoformat(),
                    "status": status,
                })
            result.append({
                "source_id": document.source_id,
                "url": document.url,
                "quote": clause.strip(),
                "windows": windows,
            })
    return result
