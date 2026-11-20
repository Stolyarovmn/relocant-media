"""Явные положительные заключения только для синтетических тестовых пакетов."""

import copy
from datetime import UTC, datetime


def facts_check_for(payload):
    outcome = payload["outcome"]
    draft = outcome["draft"]
    claims = {claim["claim_id"]: claim for claim in outcome["claims"]["claims"]}
    citations = []
    for claim_id in draft["claim_ids"]:
        for citation in claims[claim_id]["citations"]:
            if citation not in citations:
                citations.append(copy.deepcopy(citation))
    return {
        "policy_version": "draft-coverage-v1",
        "segments": [{"field": field, "text": draft[field],
                      "claim_ids": list(draft["claim_ids"]),
                      "citations": copy.deepcopy(citations), "assessment": "supported"}
                     for field in ("title", "body")],
        "publication_scope": "ordinary_local", "scope_claim_ids": [],
        "reason": "Синтетические утверждения текста подтверждены указанными цитатами.",
    }


def bind_facts_check(payload):
    for review in payload["reviews"]:
        if review["role"] == "facts":
            review["facts_check"] = facts_check_for(payload)


def reviewed_package_for(raw, source, *, title=None, body=None, now=None):
    """Полный fake package для тестов транспорта/машины с настоящими DB-снимками."""
    from test_editorial_package import bind_context, event_payload, parse

    from relocant_media.ai.models import EvidenceDocument
    from relocant_media.ai.package import draft_sha256
    from relocant_media.domain.dedup import event_key
    from relocant_media.services.editorial import _candidate, _snapshot_member

    payload = event_payload()
    payload.update(city=source.city, raw_item_id=raw.id, source_id=source.id,
                   article_id=raw.external_id,
                   evidence=EvidenceDocument.model_validate(raw, from_attributes=True).model_dump(
                       mode="json"))
    payload["event"].update(event_key=event_key(_candidate(raw)),
                            documents=[_snapshot_member(raw, source)])
    payload["outcome"]["claims"]["claims"][0].update(text=raw.body, citations=[{
        "source_id": source.id, "url": raw.url, "quote": raw.body}])
    payload["outcome"]["draft"].update(title=title or raw.title, body=body or raw.body)
    bind_facts_check(payload)
    digest = draft_sha256(parse(payload).outcome.draft)
    for review in payload["reviews"]:
        review.update(source_id=source.id, article_id=raw.external_id, draft_sha256=digest,
                      reviewed_at=(now or datetime.now(UTC)).isoformat())
    bind_context(payload)
    return payload
