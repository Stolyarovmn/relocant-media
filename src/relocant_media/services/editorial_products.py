"""Рубрики и оформление: воспроизводимый план до проверки и отправки текста."""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from relocant_media.ai.models import EvidenceDocument, RussianDraft
from relocant_media.domain.city_profile import CityEditorialProfile, load_city_profile
from relocant_media.domain.editorial_products import EditorialPresentation
from relocant_media.domain.source_locations import extract_source_maps
from relocant_media.domain.topic_tags import is_topic_footer, topic_tags
from relocant_media.services.event_place import resolve_event_place, resolve_saved_event_place
from relocant_media.services.native_map import (
    NOTICE_LAYOUT,
    build_native_map,
    validate_native_map_draft,
    verify_live_native_map,
    verify_native_map_snapshot,
)
from relocant_media.services.source_enrichment import (
    build_source_enrichment,
    plan_illustration,
    select_source_map,
    validate_against_documents,
    validate_source_map,
)
from relocant_media.storage.models import EditorialRun, Post


def recent_publications(
    session: Session, city: str, *, now: datetime,
) -> list[dict[str, Any]]:
    """Только отправленные или уже одобренные карточки этого города за семь дней."""
    observed_at = func.coalesce(Post.published_at, Post.created_at)
    rows = session.execute(select(Post, EditorialRun).join(
        EditorialRun, EditorialRun.post_id == Post.id).where(
            Post.city == city,
            observed_at >= now - timedelta(days=7), observed_at <= now,
            or_(EditorialRun.delivery_status == "sent", and_(
                EditorialRun.delivery_status == "pending",
                EditorialRun.outcome["status"].as_string() == "ready")),
        ).order_by(observed_at.desc(), Post.id.desc()))
    return [{
        "id": post.id, "title": post.title, "body": post.body,
        "status": "published" if run.delivery_status == "sent" else "ready",
        "published_at": (post.published_at or post.created_at).isoformat(),
        "rubric_id": (run.outcome.get("ranking", {}).get("selection") or {}).get("rubric_id"),
    } for post, run in rows]


def rubric_plan(
    profile: CityEditorialProfile, history: list[dict[str, Any]], *, now: datetime,
) -> dict[str, Any]:
    """Дефицит рубрик — подсказка редактору, никогда не разрешение слабой новости."""
    if now.tzinfo is None:
        raise ValueError("Время плана должно содержать часовой пояс")
    local = now.astimezone(ZoneInfo(profile.timezone))
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - timedelta(days=day.weekday())
    items = []
    for rubric in sorted(profile.rubrics, key=lambda value: (-value.priority, value.id)):
        start = day if rubric.cadence == "daily" else week
        count = 0
        for entry in history:
            if entry.get("rubric_id") != rubric.id or entry.get("status") != "published":
                continue
            observed = datetime.fromisoformat(entry["published_at"])
            if observed.tzinfo is None:
                raise ValueError("Дата публикации должна содержать часовой пояс")
            count += start <= observed.astimezone(local.tzinfo) <= local
        items.append({"rubric_id": rubric.id, "name": rubric.name,
                      "period_start": start.isoformat(), "published": count,
                      "target": rubric.target_per_period,
                      "remaining": max(0, rubric.target_per_period - count),
                      "required_details": list(rubric.required_details)})
    return {"version": 1, "city": profile.city_id, "profile_sha256": profile.sha256,
            "as_of": now.astimezone(UTC).isoformat(), "timezone": profile.timezone,
            "targets_are_not_publication_quotas": True, "rubrics": items}


def build_presentation(
    session: Session, raw_id: int, documents: tuple[EvidenceDocument, ...], *,
    document_ids: list[int], profile: CityEditorialProfile, rubric_id: str,
    source_url: str | None = None,
) -> EditorialPresentation:
    rubric = profile.rubric(rubric_id)
    if rubric is None:
        raise ValueError("Неизвестная рубрика")
    enrichment = build_source_enrichment(session, raw_id, document_ids=document_ids)
    validate_against_documents(enrichment, documents)
    source_url = source_url or _primary_url(documents)
    if source_url not in {document.url for document in documents}:
        raise ValueError("Первичный источник отсутствует в документах")
    # Несколько адресов не превращаем в одну произвольно выбранную точку.
    document = next(doc for doc in documents if doc.url == source_url)
    event_place = (resolve_saved_event_place(session, document, document_ids, profile=profile)
                   if "events" in rubric.categories and profile.city_id == "belgrade" else None)
    native_map = (build_native_map(
        session, document_ids, documents, source_url=source_url, profile=profile,
    ) if event_place is None else None)
    source_map = (select_source_map(enrichment, source_url=source_url)
                  if native_map is None and event_place is None else None)
    return EditorialPresentation(
        version=3,
        profile_sha256=profile.sha256, rubric_id=rubric.id, rubric_name=rubric.name,
        source_map=source_map,
        illustration=plan_illustration(
            enrichment, source_url=source_url,
            allow_source_preview=native_map is None or native_map.get("layout") == NOTICE_LAYOUT),
        native_map=native_map,
        event_place=event_place,
    )


def present_draft(
    draft: RussianDraft, presentation: EditorialPresentation, *, category: str = "",
) -> RussianDraft:
    """Идемпотентное оформление до двух ревью; после них текст не дополняется."""
    label = (topic_tags(category, presentation_version=presentation.version)
             if presentation.version >= 2 else
             f"Рубрика: {presentation.rubric_name}")
    lines = [line for line in draft.body.splitlines()
             if not line.startswith(("Рубрика: ", "Карта из источника: "))
             and (presentation.version == 1 or not is_topic_footer(line.strip()))]
    body = "\n".join(lines).strip()
    if presentation.source_map:
        body += f"\n\nКарта из источника: {presentation.source_map['url']}"
    body += f"\n\n{label}"
    return draft.model_copy(update={"body": body})


def _primary_url(documents: tuple[EvidenceDocument, ...]) -> str:
    return next((document.url for document in documents
                 if str(getattr(document, "ownership", "")).strip().casefold() == "official"),
                documents[0].url)


def validate_presentation(
    presentation: EditorialPresentation, profile: CityEditorialProfile,
    documents: tuple[EvidenceDocument, ...], draft: RussianDraft, *, rubric_id: str,
    category: str = "",
) -> None:
    rubric = profile.rubric(rubric_id)
    if (rubric is None or presentation.profile_sha256 != profile.sha256
            or presentation.rubric_id != rubric.id or presentation.rubric_name != rubric.name):
        raise ValueError("Редакционный профиль или рубрика изменились после проверки")
    if presentation.source_map is not None:
        validate_source_map(presentation.source_map, documents)
    if present_draft(draft, presentation, category=category) != draft:
        raise ValueError("Оформление не совпадает с проверенным текстом")
    if presentation.illustration.get("illustration_mode") not in {"source_preview", "text"}:
        raise ValueError("Вложение требует отдельного проверенного media release")
    if presentation.illustration.get("source_url") != _primary_url(documents):
        raise ValueError("Иллюстрация не соответствует первичному источнику")
    if presentation.event_place is not None:
        place = presentation.event_place
        if (profile.city_id != "belgrade" or "events" not in rubric.categories
                or presentation.source_map is not None or presentation.native_map is not None
                or place.get("timezone") != profile.timezone
                or place.get("source_url") != _primary_url(documents)):
            raise ValueError("Площадка афиши не соответствует городу и первоисточнику")
    validate_native_map_draft(presentation, draft, documents, profile=profile)


def verify_stored_presentation(
    session: Session, raw_id: int, documents: tuple[EvidenceDocument, ...], *,
    document_ids: list[int], presentation: EditorialPresentation,
) -> None:
    """Не доверяем присланному URL: повторно получаем план из сохранённого HTML."""
    enrichment = build_source_enrichment(session, raw_id, document_ids=document_ids)
    validate_against_documents(enrichment, documents)
    source_url = _primary_url(documents)
    place = presentation.event_place
    expected_map = (None if presentation.native_map is not None or place is not None
                    else select_source_map(enrichment, source_url=source_url))
    if presentation.source_map != expected_map:
        raise ValueError("Карта не совпадает с сохранённым источником")
    native = presentation.native_map
    if presentation.illustration != plan_illustration(
        enrichment, source_url=source_url,
        allow_source_preview=native is None or native.get("layout") == NOTICE_LAYOUT,
    ):
        raise ValueError("План иллюстрации изменился после проверки")
    if native is not None:
        verify_native_map_snapshot(session, native, documents, document_ids)
    if place is not None:
        document = next(doc for doc in documents if doc.url == source_url)
        profile = load_city_profile("belgrade")
        if resolve_saved_event_place(session, document, document_ids, profile=profile) != place:
            raise ValueError("Площадка афиши изменилась после проверки")


def verify_live_map(
    presentation: EditorialPresentation | None, document: EvidenceDocument,
    source_html: str | None,
) -> None:
    """Изменение ссылки при неизменном тексте тоже требует нового пакета и ревью."""
    if presentation is not None and presentation.native_map is not None:
        verify_live_native_map(presentation.native_map, document, source_html)
    if presentation is not None and (place := presentation.event_place) is not None:
        if place["source_url"] == document.url:
            actual = resolve_event_place(
                document, source_html or "", profile=load_city_profile("belgrade"),
            )
            if actual is None or any(
                actual.get(key) != value for key, value in place.items()
                if key not in {"source_html_sha256", "document_sha256"}
            ):
                raise ValueError("Площадка или расписание изменились в живом источнике")
    if presentation is None or presentation.source_map is None:
        return
    expected = presentation.source_map
    if expected["source_page"] != document.url:
        return
    candidates = extract_source_maps(source_html or "", document.url)
    grounded = []
    for candidate in candidates:
        try:
            validate_source_map(candidate, (document,))
        except ValueError:
            continue
        grounded.append(candidate)
    if (len({item["url"] for item in grounded}) != 1 or not any(
        item["url"] == expected["url"]
        and " ".join(item["source_quote"].split()) == " ".join(expected["source_quote"].split())
        for item in grounded
    )):
        raise ValueError("Карта в живом источнике изменилась или недоступна")
