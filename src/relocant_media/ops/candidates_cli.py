"""Export unreviewed staging evidence without loading runtime or AI settings."""

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session, aliased

from relocant_media.domain.dedup import EventCandidate, cluster_events, event_key, same_event
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
    assert item.published_at is not None
    return {
        "source_id": item.source_id,
        "url": item.url,
        "title": item.title,
        "body": item.body,
        "published_at": item.published_at.astimezone(UTC).isoformat(),
    }


def select_candidates(
    session: Session, *, city: str, since: datetime, until: datetime, limit: int,
    newest_first: bool = False, source_priorities: dict[str, int] | None = None,
    priority_raw_ids: set[int] | None = None,
    recent_source_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Only SELECT; the caller supplies a consistent read-only transaction."""
    if not city.strip() or not 1 <= limit <= 50:
        raise ValueError("Нужны город и лимит от 1 до 50")
    since = aware_datetime(since.isoformat())
    until = aware_datetime(until.isoformat())
    if since >= until:
        raise ValueError("Начало окна должно предшествовать концу")

    reviewed_item = aliased(RawItem)
    reviewed_url = select(reviewed_item.url).join(
        EditorialRun, EditorialRun.raw_item_id == reviewed_item.id,
    )
    base = select(RawItem, Source).join(Source, Source.id == RawItem.source_id).where(
        Source.city == city,
        Source.enabled.is_(True),
        ~select(EditorialRun.raw_item_id).where(EditorialRun.raw_item_id == RawItem.id).exists(),
        RawItem.url.not_in(reviewed_url),
    )
    eligible = base.where(RawItem.published_at >= since, RawItem.published_at < until)
    rows = session.execute(eligible.order_by(RawItem.published_at, RawItem.id)).all()

    # Compare against the full previously reviewed evidence, not only exact URLs/hashes.
    # Otherwise a later report of the same event from a second publisher could re-enter
    # the queue even though the event was already reviewed.
    reviewed_items = list(
        session.scalars(
            select(RawItem)
            .join(EditorialRun, EditorialRun.raw_item_id == RawItem.id)
            .join(Source, Source.id == RawItem.source_id)
            .where(Source.city == city)
        )
    )
    reviewed_candidates = [_event_candidate(item) for item in reviewed_items]
    reviewed_urls = {item.url for item in reviewed_items}
    reviewed_canonical = {
        candidate.resolved_canonical_url for candidate in reviewed_candidates
    }
    reviewed_hashes = {
        candidate.resolved_content_sha256 for candidate in reviewed_candidates
    }

    by_id: dict[int, tuple[RawItem, Source]] = {}
    event_inputs: list[EventCandidate] = []
    for item, source in rows:
        candidate = _event_candidate(item)
        if (
            item.url in reviewed_urls
            or candidate.resolved_canonical_url in reviewed_canonical
            or candidate.resolved_content_sha256 in reviewed_hashes
            or any(same_event(candidate, reviewed) for reviewed in reviewed_candidates)
        ):
            continue
        by_id[item.id] = (item, source)
        event_inputs.append(candidate)

    clusters = cluster_events(event_inputs)
    if newest_first:
        priorities = source_priorities or {}
        def queue_order(group: list[EventCandidate]) -> tuple[int, int, datetime | None, int]:
            item, source = by_id[group[0].raw_item_id]
            # Direct transport disruptions retain precedence over leisure mentions.
            import re
            disruption = re.search(r"obustav|izmen.*saobra|preusmer|затвор|обустав|измен.*саобра",
                                   item.title.casefold())
            private_priority = any(member.raw_item_id in (priority_raw_ids or set())
                                   for member in group)
            band = 4 if private_priority else 3 if disruption else priorities.get(source.id, 0)
            return band, -(recent_source_counts or {}).get(source.id, 0), item.published_at, item.id
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

    missing = base.where(RawItem.published_at.is_(None))
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
            "semantics": "published_at >= since AND published_at < until",
        },
        "exported_at": datetime.now(UTC).isoformat(),
        "candidate_count": len(candidates),
        "eligible_count": len(clusters),
        "truncated": len(clusters) > limit,
        "candidates": candidates,
        "deduplication": (
            "canonical URL + normalized content SHA256; cross-source title similarity "
            "within a 48h event window, including already reviewed events"
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
