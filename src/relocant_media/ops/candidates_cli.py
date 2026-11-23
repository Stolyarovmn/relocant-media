"""Export unreviewed staging evidence without loading runtime or AI settings."""

import argparse
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import and_, create_engine, func, or_, select, text
from sqlalchemy.orm import Session

from relocant_media.domain.audience import local_reader_interest, local_road_closure
from relocant_media.domain.dedup import EventCandidate, cluster_events, event_key
from relocant_media.domain.freshness import GUIDE_MAX_AGE, GUIDE_SOURCE_TYPE
from relocant_media.domain.publication_time import publication_interval
from relocant_media.services.editorial import _reviewed_event_item
from relocant_media.storage.models import EditorialRun, RawItem, Source
from relocant_media.storage.sqlite_schema import verify_sqlite_schema


def aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Требуется дата с часовым поясом")
    return parsed.astimezone(UTC)


def evidence_sha256(evidence: dict[str, Any]) -> str:
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_candidate(item: RawItem) -> EventCandidate:
    return EventCandidate(
        raw_item_id=item.id,
        source_id=item.source_id,
        url=item.url,
        title=item.title,
        body=item.body,
        published_at=item.published_at,
        canonical_url=item.canonical_url,
        content_sha256=item.content_sha256,
    )


def _evidence(item: RawItem) -> dict[str, Any]:
    return {
        "source_id": item.source_id,
        "url": item.url,
        "title": item.title,
        "body": item.body,
        "published_at": (
            item.published_at.astimezone(UTC).isoformat() if item.published_at else None
        ),
        "published_at_precision": item.published_at_precision,
    }


def select_candidates(
    session: Session, *, city: str, since: datetime, until: datetime, limit: int,
    newest_first: bool = False, source_priorities: dict[str, int] | None = None,
    priority_raw_ids: set[int] | None = None,
    recent_source_counts: dict[str, int] | None = None,
    allowed_raw_ids: set[int] | None = None,
    excluded_raw_ids: set[int] | None = None,
    locality_aliases: tuple[str, ...] = (),
    local_place_aliases: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Only SELECT; the caller supplies a consistent read-only transaction."""
    if not city.strip() or not 1 <= limit <= 50:
        raise ValueError("Нужны город и лимит от 1 до 50")
    since = aware_datetime(since.isoformat())
    until = aware_datetime(until.isoformat())
    if since >= until:
        raise ValueError("Начало окна должно предшествовать концу")

    base = select(RawItem, Source).join(Source, Source.id == RawItem.source_id).where(
        Source.city == city,
        Source.enabled.is_(True),
        ~select(EditorialRun.raw_item_id).where(EditorialRun.raw_item_id == RawItem.id).exists(),
    )
    if allowed_raw_ids is not None:
        base = base.where(RawItem.id.in_(allowed_raw_ids))
    if excluded_raw_ids:
        base = base.where(RawItem.id.not_in(excluded_raw_ids))
    # A day boundary is not the publication instant. Include overlapping days
    # (allow 26 hours for DST), plus newly discovered dated articles, then check
    # the real local calendar interval below. All publication guards still run.
    eligible = base.where(RawItem.published_at.is_not(None), RawItem.published_at < until, or_(
        RawItem.published_at >= since,
        and_(RawItem.published_at_precision == "day", or_(
            RawItem.published_at >= since - timedelta(hours=26),
            and_(RawItem.ingested_at >= since, RawItem.ingested_at < until),
        )),
    ))
    queried = session.execute(eligible.order_by(RawItem.published_at, RawItem.id)).all()
    window_matches: dict[int, str] = {}
    retained = []
    for item, source in queried:
        assert item.published_at is not None
        if item.published_at_precision == "day":
            lower, upper = publication_interval(item.published_at, "day")
            if lower < until and upper > since:
                match = "calendar_day_overlap"
            elif since <= item.ingested_at < until:
                match = "late_discovery"
            else:
                continue
        else:
            match = ("exact_timestamp" if item.published_at_precision == "instant"
                     else "stored_timestamp_precision_unknown")
        window_matches[item.id] = match
        retained.append((item, source))
    # Evergreen guides have no publication date by design: they are valid for 30 days from
    # the moment their text was first seen, whatever the editorial window is.
    guides = session.execute(base.where(
        Source.source_type == GUIDE_SOURCE_TYPE, RawItem.published_at.is_(None),
        RawItem.ingested_at <= until, RawItem.ingested_at >= until - GUIDE_MAX_AGE,
    ).order_by(RawItem.id)).all()
    for item, source in guides:
        window_matches[item.id] = "guide_verified"
        retained.append((item, source))
    rows = retained

    by_id: dict[int, tuple[RawItem, Source]] = {}
    event_inputs: list[EventCandidate] = []
    for item, source in rows:
        # Тот же read-only proof, что и перед полным pipeline. Старый отказ/закрытие
        # не подавляет свежую тему; реальные квитанции и uncertain остаются защитой.
        # Helper предварительно сужает SQL до Post/transport, не читая архив skipped.
        with session.no_autoflush:
            if _reviewed_event_item(session, anchor=item, anchor_source=source) is not None:
                continue
        by_id[item.id] = (item, source)
        event_inputs.append(_event_candidate(item))

    clusters = cluster_events(event_inputs)
    local_closure_ids: set[int] = set()
    if newest_first:
        priorities = source_priorities or {}
        # Только настроенные местные ориентиры; название общенациональной организации
        # не подставляется вместо географии. Это порядок рассмотрения, не доказательство.
        locality_names = (*locality_aliases, *local_place_aliases)
        locality = re.compile(
            r"(?<!\w)(?:" + "|".join(re.escape(alias.casefold()) for alias in locality_names)
            + r")\w*", re.I,
        ) if locality_names else None
        practical = re.compile(
            r"saobrać|prevoz|isklju|bez vode|bez struje|grejanj|letov|otkaz|"
            r"avio|aerodrom|kašnjen|poremećaj|poplav|kiš|vremensk|nevrem|"
            r"izložb|koncert|festival|vaučer|радов|превоз|искључ|грејањ|"
            r"изложб|концерт|фестивал|отключ|перекры|выставк|рейс|"
            r"авио|аеродром|кашњењ|поплав|киш|непогод|временск|затоп|погод", re.I,
        )
        if locality is not None:
            local_closure_ids = {
                item.id for item, source in by_id.values()
                if source.source_type == "news_media" and source.ownership == "media"
                and local_road_closure(item.title, item.body, locality)
            }

        def queue_order(
            group: list[EventCandidate],
        ) -> tuple[int, int, int, int, datetime | None, int]:
            item, source = by_id[group[0].raw_item_id]
            # Direct transport disruptions retain precedence over leisure mentions.
            disruption = re.search(r"obustav|izmen.*saobra|preusmer|затвор|обустав|измен.*саобра",
                                   item.title.casefold())
            private_priority = any(member.raw_item_id in (priority_raw_ids or set())
                                   for member in group)
            guide = source.source_type == GUIDE_SOURCE_TYPE
            lead = item.title + "\n" + item.body[:500]
            media = source.source_type == "news_media"
            local = bool(locality and locality.search(lead))
            local_practical = bool(media and local and (
                practical.search(lead)
                or local_reader_interest(item.title, item.body, locality_names)))
            media_closure = item.id in local_closure_ids
            # Национальный поток не получает транспортный приоритет только из-за
            # слова «отмена» о чужом городе. Локальная практическая новость СМИ
            # не остаётся навечно ниже любой общей официальной заметки.
            band = (4 if private_priority or guide else
                    3 if media_closure or disruption and (not media or local) else
                    max(0, priorities.get(source.id, 0)) if local_practical else
                    priorities.get(source.id, 0))
            return (band, int(band == 3 and bool(disruption) and not media),
                    int(local_practical or media_closure),
                    -(recent_source_counts or {}).get(source.id, 0),
                    item.published_at or item.ingested_at, item.id)
        clusters.sort(key=queue_order, reverse=True)
    candidates = []
    for cluster in clusters[:limit]:
        representative = cluster[0]
        item, source = by_id[representative.raw_item_id]
        evidence = _evidence(item)
        event_evidence = []
        source_ids: list[str] = []
        for member in cluster:
            member_item, member_source = by_id[member.raw_item_id]
            event_evidence.append({
                "raw_item_id": member_item.id,
                "ownership": member_source.ownership,
                "trust": str(member_source.trust),
                "evidence": _evidence(member_item),
            })
            if member_item.source_id not in source_ids:
                source_ids.append(member_item.source_id)
        candidates.append({
            "raw_item_id": item.id,
            "article_id": item.external_id,
            "source_id": item.source_id,
            "window_match": window_matches[item.id],
            "evidence": evidence,
            "evidence_sha256": evidence_sha256(evidence),
            "source": {
                "name": source.name,
                "url": source.url,
                "type": source.source_type,
                "ownership": source.ownership,
                "auto_publish": source.auto_publish,
                "high_risk": source.high_risk,
            },
            "event": {
                "key": event_key(representative),
                "member_count": len(cluster),
                "member_raw_item_ids": [member.raw_item_id for member in cluster],
                "source_ids": source_ids,
                "evidence": event_evidence,
            },
        })
        if item.id in local_closure_ids:
            candidates[-1]["local_road_closure"] = True

    missing = base.where(RawItem.published_at.is_(None), Source.source_type != GUIDE_SOURCE_TYPE)
    missing_count = session.scalar(select(func.count()).select_from(missing.subquery())) or 0
    excluded = [{
        "raw_item_id": item.id,
        "source_id": item.source_id,
        "article_id": item.external_id,
        "url": item.url,
        "status": "excluded_missing_published_at",
    } for item, _source in session.execute(missing.order_by(RawItem.id).limit(limit)).all()]
    return {
        "version": 2,
        "city": city,
        "window": {
            "since": since.isoformat(),
            "until": until.isoformat(),
            "semantics": "timestamp window; calendar-day overlap or newly ingested day-only item",
        },
        "exported_at": datetime.now(UTC).isoformat(),
        "candidate_count": len(candidates),
        "eligible_count": len(clusters),
        "truncated": len(clusters) > limit,
        "candidates": candidates,
        "deduplication": (
            "canonical URL + normalized content SHA256; cross-source title similarity "
            "within a 48h event window; previous events require verified snapshot "
            "or transport proof"
        ),
        "missing_published_at_count": missing_count,
        "missing_published_at_scope": "all unreviewed enabled city sources, outside dated window",
        "excluded_missing_published_at": excluded,
        "missing_published_at_truncated": missing_count > limit,
    }


def export_candidates(
    *, city: str, since: datetime, until: datetime, limit: int, output: Path,
) -> dict[str, Any]:
    if os.environ.get("APP_ENV", "").lower() != "staging":
        raise ValueError("Экспорт разрешён только при APP_ENV=staging")
    if output.exists():
        raise ValueError("Каталог выпуска уже существует; перезапись запрещена")
    if not city.strip() or not 1 <= limit <= 50:
        raise ValueError("Нужны город и лимит от 1 до 50")
    since = aware_datetime(since.isoformat())
    until = aware_datetime(until.isoformat())
    if since >= until:
        raise ValueError("Начало окна должно предшествовать концу")
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL не задан")
    engine = create_engine(database_url)
    try:
        if engine.dialect.name == "postgresql":
            engine = engine.execution_options(isolation_level="REPEATABLE READ")
        elif engine.dialect.name == "sqlite":
            verify_sqlite_schema(engine)
        else:
            raise ValueError("unsupported export database")
        with Session(engine) as session, session.begin():
            if engine.dialect.name == "sqlite":
                session.execute(text("PRAGMA query_only=ON"))
                session.execute(text("BEGIN"))
            else:
                session.execute(text("SET TRANSACTION READ ONLY"))
            result = select_candidates(session, city=city, since=since, until=until, limit=limit)
        # Exclusive directory creation prevents a concurrent export from overwriting evidence.
        output.mkdir(parents=True, exist_ok=False)
        with (output / "manifest.json").open("x", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
        return result
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Экспорт кандидатов для независимого ревью")
    parser.add_argument("--city", required=True)
    parser.add_argument("--since", required=True, type=aware_datetime)
    parser.add_argument("--until", required=True, type=aware_datetime)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = export_candidates(**vars(args))
    except Exception:
        # Database/driver exceptions can contain credentials; never print their contents.
        parser.exit(1, "Экспорт не выполнен: проверьте staging, параметры, БД и новый каталог.\n")
    print(json.dumps({
        "candidate_count": result["candidate_count"],
        "missing_published_at_count": result["missing_published_at_count"],
        "manifest": str(args.output / "manifest.json"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
