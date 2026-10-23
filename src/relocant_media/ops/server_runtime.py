"""Production server entrypoint with tightly scoped direct official-source extensions.

The base newsroom remains unchanged. This adapter adds the Government of Serbia direct-link
source to private intake and live owner-review verification without turning the government
news feed into autonomous discovery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.ai.package import EditorialPackage
from relocant_media.collectors.srbija_gov_article import (
    SrbijaGovArticleCollector,
    canonical_srbija_gov_article_url,
    match_srbija_gov_article_url,
    parse_srbija_gov_article,
)
from relocant_media.domain.freshness import freshness_decision
from relocant_media.ingestion.persistence import capture_collected_batch, sync_source
from relocant_media.ops import server_cycle as base
from relocant_media.services.published_watch import evidence_changed, published_sources
from relocant_media.services.submission_feedback import resolve_submission
from relocant_media.services.submission_queue import batch_submissions, pending_submissions, tip_urls
from relocant_media.sources.registry import SourceConfig
from relocant_media.storage.models import EditorialRun, RawItem, Submission

SOURCES = Path("cities/belgrade/sources")
SOURCE_ID = "srbija_gov_news"
_BASE_COLLECT_SOURCES = base.collect_sources
_BASE_FETCH_DOCUMENTS = base.fetch_documents


def _source() -> SourceConfig:
    return base.load_source_file(SOURCES / f"{SOURCE_ID}.yaml")


def _base_direct_sources() -> dict[str, SourceConfig]:
    sources = {
        key: base.load_source_file(SOURCES / f"{key}.yaml")
        for key in ("city_beograd_news", "cukarica_news")
    }
    sources.update(base.venue_sources())
    sources.update(base.review_only_venue_sources())
    return sources


def _direct_budget(rows: list[Submission]) -> int:
    """Keep the existing maximum of ten independently fetched private links per pass."""
    known = _base_direct_sources()
    already_allowed: set[str] = set()
    for row in rows:
        for url in tip_urls(row.payload):
            if base.source_for_url(url, known) is not None:
                already_allowed.add(url)
    return max(0, 10 - len(already_allowed))


async def _collect_direct_tips(
    engine: Engine,
    client: httpx.AsyncClient,
    *,
    submissions_only: bool,
    now: datetime,
) -> dict[str, Any]:
    source = _source()
    with Session(engine) as session:
        sync_source(session, "belgrade", source)
        session.commit()
        rows = batch_submissions(session) if submissions_only else pending_submissions(session)
        selected = [(row.id, row.payload) for row in rows]
        budget = _direct_budget(rows)

    url_to_submissions: dict[str, set[int]] = {}
    for submission_id, payload in selected:
        for url in tip_urls(payload):
            if match_srbija_gov_article_url(url) is not None:
                url_to_submissions.setdefault(url, set()).add(submission_id)

    collector = SrbijaGovArticleCollector(source, client)
    fetched = failures = stale = 0
    items = []
    external_to_submissions: dict[str, set[int]] = {}
    for url, submission_ids in list(url_to_submissions.items())[:budget]:
        try:
            item = await collector.collect_url(url)
            fresh = freshness_decision(item.title, item.body, item.published_at, now)
            if fresh.action != "keep":
                stale += 1
                continue
        except (httpx.HTTPError, ValueError):
            failures += 1
            continue
        fetched += 1
        items.append(item)
        external_to_submissions.setdefault(item.external_id, set()).update(submission_ids)

    written = review_required = 0
    raw_ids: dict[int, list[int]] = {}
    if items:
        with Session(engine) as session:
            written, review_required = capture_collected_batch(session, items)
            session.commit()
            for item in items:
                raw_id = session.scalar(
                    select(RawItem.id).where(
                        RawItem.source_id == SOURCE_ID,
                        RawItem.external_id == item.external_id,
                    )
                )
                if raw_id is None:
                    raise RuntimeError("Government of Serbia raw item did not persist")
                for submission_id in external_to_submissions[item.external_id]:
                    raw_ids.setdefault(submission_id, []).append(raw_id)

    return {
        "links_seen": len(url_to_submissions),
        "fetched": fetched,
        "stale": stale,
        "failures": failures,
        "written": written,
        "review_required": review_required,
        "raw_ids": raw_ids,
    }


async def _watch_published(
    engine: Engine,
    client: httpx.AsyncClient,
    *,
    now: datetime,
) -> dict[str, int]:
    source = _source()
    with Session(engine) as session:
        candidates = [
            row for row in published_sources(session, now=now) if row.source_id == SOURCE_ID
        ]
    if not candidates:
        return {"checked": 0, "changed": 0, "failures": 0}
    collector = SrbijaGovArticleCollector(source, client)
    changed_items = []
    checked = failures = 0
    for original in candidates:
        try:
            current = await collector.collect_url(original.url)
        except (httpx.HTTPError, ValueError):
            failures += 1
            continue
        checked += 1
        if evidence_changed(original, current):
            changed_items.append(current)
    if changed_items:
        with Session(engine) as session:
            capture_collected_batch(session, changed_items)
            session.commit()
    return {"checked": checked, "changed": len(changed_items), "failures": failures}


def _merge_submission_links(
    engine: Engine,
    raw_ids: dict[int, list[int]],
    *,
    now: datetime,
) -> tuple[int, list[int]]:
    newly_linked = 0
    all_raw_ids: list[int] = []
    if not raw_ids:
        return newly_linked, all_raw_ids
    with Session(engine) as session:
        for submission_id, ids in raw_ids.items():
            row = session.get(Submission, submission_id)
            if row is None:
                continue
            existing = [
                value for value in row.payload.get("raw_item_ids", []) if type(value) is int
            ]
            combined = list(dict.fromkeys([*existing, *ids]))
            resolve_submission(row, combined, now=now)
            if combined and session.scalar(
                select(EditorialRun.raw_item_id)
                .where(EditorialRun.raw_item_id.in_(combined))
                .limit(1)
            ) is not None:
                row.payload = {**row.payload, "triage_reason": "already_considered"}
            newly_linked += int(bool(combined) and not existing)
            all_raw_ids.extend(ids)
        session.commit()
    return newly_linked, list(dict.fromkeys(all_raw_ids))


async def collect_sources(
    engine: Engine,
    *,
    output: Path | None = None,
    submissions_only: bool = False,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        direct = await _collect_direct_tips(
            engine,
            client,
            submissions_only=submissions_only,
            now=now,
        )
        watch = (
            {"checked": 0, "changed": 0, "failures": 0}
            if submissions_only
            else await _watch_published(engine, client, now=now)
        )

    result = await _BASE_COLLECT_SOURCES(
        engine,
        output=output,
        submissions_only=submissions_only,
    )
    newly_linked, direct_raw_ids = _merge_submission_links(
        engine,
        direct.pop("raw_ids"),
        now=now,
    )
    result["sources_synced"] = int(result.get("sources_synced", 0)) + 1
    result["raw_items_written"] = int(result.get("raw_items_written", 0)) + direct["written"]
    result["review_required"] = int(result.get("review_required", 0)) + direct["review_required"]
    result["priority_submissions"] = int(result.get("priority_submissions", 0)) + newly_linked
    result["private_link_fetches"] = int(result.get("private_link_fetches", 0)) + direct["fetched"]
    result["submission_raw_ids"] = list(
        dict.fromkeys([*result.get("submission_raw_ids", []), *direct_raw_ids])
    )
    result["srbija_gov_direct"] = {**direct, "linked_submissions": newly_linked}
    result["srbija_gov_watch"] = watch
    return result


def _fetch_srbija_document(document: EvidenceDocument) -> EvidenceDocument:
    source = _source()
    current, article_id = canonical_srbija_gov_article_url(document.url)
    with httpx.Client(timeout=25, trust_env=False) as client:
        for _ in range(6):
            response = client.get(current, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise ValueError("Government of Serbia redirect without location")
                current, redirected_id = canonical_srbija_gov_article_url(
                    urljoin(current, location)
                )
                if redirected_id != article_id:
                    raise ValueError("Government of Serbia redirect changed article ID")
                continue
            response.raise_for_status()
            item = parse_srbija_gov_article(response.text, source, current)
            return EvidenceDocument(
                source_id=item.source_id,
                url=item.url,
                title=item.title,
                body=item.body,
                published_at=item.published_at,
            )
    raise ValueError("too many Government of Serbia redirects")


def fetch_documents(package: EditorialPackage) -> tuple[EvidenceDocument, ...]:
    """Live-refetch all evidence, adding the direct Government of Serbia source."""
    assert package.event is not None
    original = list(package.event.documents)
    other = [document for document in original if document.source_id != SOURCE_ID]
    other_live: list[EvidenceDocument] = []
    if other:
        reduced_event = package.event.model_copy(update={"documents": other})
        reduced = package.model_copy(update={"event": reduced_event})
        other_live = list(_BASE_FETCH_DOCUMENTS(reduced))
    other_iter = iter(other_live)
    live: list[EvidenceDocument] = []
    for document in original:
        if document.source_id == SOURCE_ID:
            live.append(_fetch_srbija_document(document))
        else:
            live.append(next(other_iter))
    try:
        next(other_iter)
    except StopIteration:
        pass
    else:
        raise ValueError("live evidence result count mismatch")
    return tuple(live)


def main() -> None:
    # The base module resolves these names at call time, so both normal preparation and the
    # runtime collection path use the same source extension without duplicating the newsroom.
    base.collect_sources = collect_sources
    base.fetch_documents = fetch_documents
    base.main()


if __name__ == "__main__":
    main()
