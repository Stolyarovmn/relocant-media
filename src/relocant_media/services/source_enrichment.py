"""Воспроизводимые дополнения из неизменного источника для автора и двух ревью."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument
from relocant_media.collectors.article_media import (
    ArticleMediaError,
    extract_article_media_candidates,
)
from relocant_media.domain.source_locations import (
    extract_source_maps,
    is_public_https_url,
    map_provider,
)
from relocant_media.services.cover_selection import CoverAssessment
from relocant_media.services.media_review_packet import (
    _valid_snapshot,
    build_media_review_packet,
    plan_reviewed_packet,
)
from relocant_media.services.production_media import PreparedProductionMedia
from relocant_media.storage.media_models import RawItemMediaSnapshot
from relocant_media.storage.models import EditorialRun, RawItem


def _body_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _quote_in_body(quote: str, body: str) -> bool:
    return bool(quote.strip()) and " ".join(quote.split()) in " ".join(body.split())


def validate_source_map(candidate: dict[str, Any], documents: Iterable[EvidenceDocument]) -> None:
    """Проверяем ссылку и цитату без обращения к карте или геокодеру."""
    provider = map_provider(candidate.get("url", ""))
    matching = [doc for doc in documents if doc.url == candidate.get("source_page")]
    if (
        not provider
        or provider != candidate.get("provider")
        or candidate.get("location_verified") is not False
        or candidate.get("kind") != "source_map_link"
        or not re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("source_html_sha256", "")))
        or not isinstance(candidate.get("source_quote"), str)
        or not any(_quote_in_body(candidate["source_quote"], doc.body) for doc in matching)
    ):
        raise ValueError("source map URL or quote is not supported by exact evidence")


def build_source_enrichment(
    session: Session, raw_item_id: int, *, document_ids: Iterable[int] = (),
) -> dict[str, Any]:
    """Повторное извлечение из сохранённого HTML; никаких новых сетевых запросов."""
    ids = [raw_item_id, *document_ids]
    run = session.get(EditorialRun, raw_item_id)
    if run is not None:
        ids.extend(
            document["raw_item_id"] for document in run.evidence.get("documents", [])
            if isinstance(document, dict) and isinstance(document.get("raw_item_id"), int)
        )
    documents: list[dict[str, Any]] = []
    for document_id in dict.fromkeys(ids):
        raw = session.get(RawItem, document_id)
        if raw is None:
            continue
        item: dict[str, Any] = {
            "raw_item_id": raw.id,
            "source_id": raw.source_id,
            "url": raw.url,
            "title": raw.title,
            "body_sha256": _body_sha256(raw.body),
            "source_html_sha256": None,
            "map_candidates": [],
            "media_candidates": [],
            "reason": "source_html_missing",
        }
        snapshot = session.get(RawItemMediaSnapshot, document_id)
        if snapshot is not None:
            item["reason"] = "source_snapshot_invalid"
        if (
            snapshot is not None
            and snapshot.source_html is not None
            and snapshot.source_page == raw.url
            and is_public_https_url(raw.url)
            and _valid_snapshot(snapshot)
        ):
            item["source_html_sha256"] = snapshot.source_html_sha256
            item["map_candidates"] = [
                candidate for candidate in extract_source_maps(snapshot.source_html, raw.url)
                if _quote_in_body(candidate["source_quote"], raw.body)
            ]
            try:
                candidates = extract_article_media_candidates(
                    snapshot.source_html,
                    raw.url,
                    approved_image_hosts=frozenset(
                        snapshot.manifest.get("approved_image_hosts", [])
                    ),
                )
                item["media_candidates"] = [
                    asdict(candidate) for candidate in candidates
                    if is_public_https_url(candidate.url)
                ]
            except (ArticleMediaError, TypeError):
                item["media_candidates"] = []
            item["reason"] = "saved_source_html"
        documents.append(item)
    return {
        "version": 1,
        "raw_item_id": raw_item_id,
        "documents": documents,
        "network_access_performed": False,
        "generated_media_used": False,
        "publication_permitted": False,
    }


def validate_against_documents(
    enrichment: dict[str, Any], documents: Iterable[EvidenceDocument],
) -> None:
    """Не позволяем дополнениям пережить замену исходного документа под тем же URL."""
    known = {(document.source_id, document.url): document for document in documents}
    for item in enrichment.get("documents", []):
        document = known.get((item.get("source_id"), item.get("url")))
        if document is None or (
            item.get("title") != document.title
            or item.get("body_sha256") != _body_sha256(document.body)
        ):
            raise ValueError("source enrichment does not match exact evidence documents")
        for candidate in item.get("map_candidates", []):
            validate_source_map(candidate, known.values())
            if (
                candidate.get("source_page") != document.url
                or candidate.get("source_html_sha256") != item.get("source_html_sha256")
                or map_provider(candidate.get("url", "")) != candidate.get("provider")
                or candidate.get("location_verified") is not False
                or candidate.get("kind") != "source_map_link"
            ):
                raise ValueError("source map provenance does not match evidence")


def select_source_map(
    enrichment: dict[str, Any], *, source_url: str | None = None, map_url: str | None = None,
) -> dict[str, Any] | None:
    """Выбираем одну ссылку; несколько разных мест требуют явного выбора редактора."""
    candidates = [
        candidate
        for document in enrichment.get("documents", [])
        if source_url is None or document.get("url") == source_url
        for candidate in document.get("map_candidates", [])
        if (map_url is None or candidate.get("url") == map_url)
        and map_provider(candidate.get("url", "")) is not None
        and map_provider(candidate.get("url", "")) == candidate.get("provider")
        and re.fullmatch(r"[0-9a-f]{64}", str(document.get("source_html_sha256", "")))
        and candidate.get("location_verified") is False
        and candidate.get("source_page") == document.get("url")
        and candidate.get("source_html_sha256") == document.get("source_html_sha256")
        and candidate.get("kind") == "source_map_link"
    ]
    unique = {candidate["url"]: candidate for candidate in candidates}
    return dict(next(iter(unique.values()))) if len(unique) == 1 else None


def plan_illustration(
    enrichment: dict[str, Any], *, source_url: str, allow_source_preview: bool = True,
    prepared_media: PreparedProductionMedia | None = None,
) -> dict[str, Any]:
    """План представления не заменяет проверки прав, файлов и полного media release."""
    documents = [item for item in enrichment.get("documents", []) if item.get("url") == source_url]
    candidates = [candidate for item in documents for candidate in item.get("media_candidates", [])]
    base: dict[str, Any] = {
        "version": 1,
        "illustration_mode": "text",
        "reason": "source_preview_disabled_or_source_unavailable",
        "source_url": source_url,
        "asset_refs": [],
        "candidate_count": len(candidates),
        "attachment_status": "awaiting_explicit_rights_and_two_reviews" if candidates else "absent",
        "requires_independent_media_reviews": bool(candidates),
        "generated_media_used": False,
    }
    if prepared_media is not None:
        # Объект выдаёт prepare_production_media только после полных проверок файлов и аттестаций.
        if prepared_media.release.raw_item_id != enrichment.get("raw_item_id"):
            raise ValueError("prepared media does not match source enrichment")
        base.update(
            illustration_mode="reviewed_attachment",
            reason="validated_production_media_release",
            attachment_status="reviewed",
            release_sha256=prepared_media.release_sha256,
            asset_refs=[asset.model_dump(mode="json") for asset in prepared_media.release.assets],
            requires_independent_media_reviews=True,
        )
    elif documents and allow_source_preview and is_public_https_url(source_url):
        base.update(
            illustration_mode="source_preview",
            reason="exact_source_url_preview_no_attachment_or_reuse_rights_granted",
            preview_url=source_url,
        )
    return base


def build_media_preparation_packet(
    session: Session,
    raw_item_id: int,
    *,
    assessments: tuple[CoverAssessment, ...] = (),
    event_year: int | None = None,
) -> dict[str, Any]:
    """Точный следующий шаг после импорта: кандидаты, права, выбор одной обложки, две проверки."""
    if assessments and event_year is None:
        raise ValueError("explicit event year required with cover assessments")
    packet = build_media_review_packet(session, raw_item_id)
    enrichment = build_source_enrichment(session, raw_item_id)
    extracted = {
        (candidate["url"], candidate["source_page"])
        for document in enrichment["documents"]
        for candidate in document["media_candidates"]
    }
    # Manifest не может подменить кандидата, отсутствующего в сохранённом HTML.
    packet["candidates"] = [
        item for item in packet.get("candidates", [])
        if (item["candidate"]["url"], item["candidate"]["source_page"]) in extracted
    ]
    if not packet["candidates"]:
        packet.update(mode="text", reason="no_reviewable_media_candidates")
    cover_plan: dict[str, object]
    if event_year is not None:
        cover_plan = plan_reviewed_packet(packet, assessments, event_year=event_year)
    else:
        cover_plan = {
            "mode": "text", "selected": [], "rejected": [],
            "reason": "explicit_assessments_and_event_year_required",
            "requires_independent_media_reviews": bool(packet["candidates"]),
        }
    refs = [{
        "image_url": item["candidate"]["url"],
        "source_page": item["candidate"]["source_page"],
        "source_raw_item_id": item["source_raw_item_id"],
        "source_html_sha256": item["source_html_sha256"],
        "materialized_asset_sha256": None,
    } for item in packet["candidates"]]
    selected = cover_plan.get("selected", [])
    selected_urls = {
        item["image_url"] for item in selected if isinstance(item, dict)
    } if isinstance(selected, list) else set()
    selected_refs = [ref for ref in refs if ref["image_url"] in selected_urls]
    status = (
        "awaiting_materialization_and_two_reviews" if selected_refs
        else "awaiting_explicit_cover_assessments" if refs else "text"
    )
    next_cli = (
        ["relocant-media-assets", "--raw-item-id", str(raw_item_id),
         "--assessments", "assessments.json", "--event-year", str(event_year),
         "--output", f"media-assets-{raw_item_id}"]
        if selected_refs else
        ["relocant-media-review", "--raw-item-id", str(raw_item_id),
         "--output", f"media-review-{raw_item_id}"] if refs else []
    )
    return {
        "version": 1,
        "raw_item_id": raw_item_id,
        "post_id": packet.get("post_id"),
        "editorial_package_sha256": packet.get("editorial_package_sha256"),
        "status": status,
        "review_packet": packet,
        "candidate_refs": refs,
        "selected_asset_refs": selected_refs,
        "cover_plan": cover_plan,
        "next_cli_argv": next_cli,
        "required_environment": {"APP_ENV": "staging", "DATABASE_URL": "explicit_local_sqlite"},
        "required_reviews": ["facts", "utility"],
        "reviews_bind": ["exact_asset_bytes", "full_caption", "target", "editorial_package_sha256"],
        "rights_inferred": False,
        "network_access_performed": False,
        "download_permitted": False,
        "publication_permitted": False,
    }
