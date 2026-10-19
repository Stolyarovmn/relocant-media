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

from relocant_media.storage.models import EditorialRun, RawItem, Source


def aware_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Требуется дата с часовым поясом")
    return parsed.astimezone(UTC)


def evidence_sha256(evidence: dict[str, Any]) -> str:
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def select_candidates(
    session: Session, *, city: str, since: datetime, until: datetime, limit: int,
) -> dict[str, Any]:
    """Only SELECT; the caller supplies a consistent read-only transaction."""
    if not city.strip() or not 1 <= limit <= 50:
        raise ValueError("Нужны город и лимит от 1 до 50")
    since = aware_datetime(since.isoformat())
    until = aware_datetime(until.isoformat())
    if since >= until:
        raise ValueError("Начало окна должно предшествовать концу")
    reviewed_item = aliased(RawItem)
    # URLs must already be canonicalized by collectors; deliberately no fuzzy matching.
    reviewed_url = select(reviewed_item.url).join(
        EditorialRun, EditorialRun.raw_item_id == reviewed_item.id,
    )
    base = select(RawItem, Source).join(Source, Source.id == RawItem.source_id).where(
        Source.city == city, Source.enabled.is_(True),
        ~select(EditorialRun.raw_item_id).where(EditorialRun.raw_item_id == RawItem.id).exists(),
        RawItem.url.not_in(reviewed_url),
    )
    eligible = base.where(RawItem.published_at >= since, RawItem.published_at < until)
    ranked = eligible.with_only_columns(
        RawItem.id,
        func.row_number().over(
            partition_by=RawItem.url, order_by=(RawItem.published_at, RawItem.id),
        ).label("url_rank"),
    ).subquery()
    unique_ids = select(ranked.c.id).where(ranked.c.url_rank == 1)
    eligible = base.where(RawItem.id.in_(unique_ids))
    count = session.scalar(select(func.count()).select_from(eligible.subquery())) or 0
    rows = session.execute(
        eligible.order_by(RawItem.published_at, RawItem.id).limit(limit)
    ).all()
    candidates = []
    for item, source in rows:
        assert item.published_at is not None
        evidence = {
            "source_id": item.source_id, "url": item.url, "title": item.title,
            "body": item.body,
            "published_at": item.published_at.astimezone(UTC).isoformat(),
        }
        candidates.append({
            "raw_item_id": item.id, "article_id": item.external_id,
            "source_id": item.source_id, "evidence": evidence,
            "evidence_sha256": evidence_sha256(evidence),
            "source": {"name": source.name, "url": source.url, "type": source.source_type},
        })
    missing = base.where(RawItem.published_at.is_(None))
    missing_count = session.scalar(select(func.count()).select_from(missing.subquery())) or 0
    excluded = [{
        "raw_item_id": item.id, "source_id": item.source_id,
        "article_id": item.external_id, "url": item.url,
        "status": "excluded_missing_published_at",
    } for item, _source in session.execute(missing.order_by(RawItem.id).limit(limit)).all()]
    return {
        "version": 1, "city": city,
        "window": {"since": since.isoformat(), "until": until.isoformat(),
                   "semantics": "published_at >= since AND published_at < until"},
        "exported_at": datetime.now(UTC).isoformat(),
        "candidate_count": len(candidates), "eligible_count": count,
        "truncated": count > limit, "candidates": candidates,
        "deduplication": "exact URL within window; earliest published_at then raw_item_id",
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
    engine = create_engine(database_url, isolation_level="REPEATABLE READ")
    try:
        if engine.dialect.name != "postgresql":
            raise ValueError("Экспорт требует PostgreSQL")
        with Session(engine) as session, session.begin():
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
    print(json.dumps({"candidate_count": result["candidate_count"],
                      "missing_published_at_count": result["missing_published_at_count"],
                      "manifest": str(args.output / "manifest.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
