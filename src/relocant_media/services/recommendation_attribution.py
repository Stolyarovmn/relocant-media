"""Turn observed daily-report recommendations into auditable GitHub-ready proposals.

The service never changes editorial rules, opens GitHub resources itself, or invents missing
metrics. GitHub issue/PR creation remains an external action whose target is linked back explicitly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import DailyReport
from relocant_media.storage.recommendation_control import (
    RecommendationProposal,
    RecommendationTargetLink,
)

KNOWN_PR_REVIEW_CODES = frozenset({"source_failures", "ai_errors"})
ALLOWED_TARGET_KINDS = frozenset({"issue", "pr"})


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _mapping(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"daily report {field} must be a mapping")
    return value


def _retained_cpl_evidence(acquisition: dict[str, Any]) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []
    for horizon in ("d1", "d7", "d30"):
        raw = acquisition.get(f"retained_cpl_{horizon}", {})
        if not isinstance(raw, dict):
            raise ValueError(f"retained_cpl_{horizon} must be a mapping")
        if not raw:
            unavailable.append({"horizon": horizon, "reason": "no_observed_cohorts"})
            continue
        usable = 0
        for currency, metrics in sorted(raw.items()):
            if not isinstance(currency, str) or not isinstance(metrics, dict):
                raise ValueError("retained CPL entries must be currency mappings")
            retained = metrics.get("retained")
            cpl = metrics.get("cpl")
            spend = metrics.get("spend")
            cohorts = metrics.get("cohorts")
            if cpl is None:
                unavailable.append(
                    {
                        "horizon": horizon,
                        "currency": currency,
                        "reason": "retained_cpl_unavailable",
                    }
                )
                continue
            if type(retained) is not int or retained <= 0:
                raise ValueError("non-null retained CPL requires positive retained count")
            if not isinstance(cpl, str) or not isinstance(spend, str) or type(cohorts) is not int:
                raise ValueError("retained CPL observation is incomplete")
            observations.append(
                {
                    "horizon": horizon,
                    "currency": currency,
                    "cohorts": cohorts,
                    "spend": spend,
                    "retained": retained,
                    "cpl": cpl,
                }
            )
            usable += 1
        if usable == 0 and not any(row["horizon"] == horizon for row in unavailable):
            unavailable.append({"horizon": horizon, "reason": "no_usable_observation"})
    return {
        "observations": observations,
        "unavailable": unavailable,
        "currencies_combined": False,
        "synthetic_zero_used": False,
    }


def _recommendation_metrics(code: str, report: dict[str, Any]) -> dict[str, Any]:
    if code == "source_failures":
        sources = _mapping(report.get("sources"), "sources")
        return {
            "collector_failures": sources.get("collector_failures"),
            "raw_items_ingested": sources.get("raw_items_ingested"),
        }
    if code == "ai_errors":
        ai = _mapping(report.get("ai"), "ai")
        return {
            "events": ai.get("events"),
            "errors": ai.get("errors"),
            "token_coverage_complete": ai.get("token_coverage_complete"),
            "cost_events": ai.get("cost_events"),
            "costs_observed": ai.get("costs_observed"),
        }
    if code == "collect_audience_samples":
        audience = _mapping(report.get("audience"), "audience")
        return {
            "snapshots": audience.get("snapshots"),
            "first_subscribers": audience.get("first_subscribers"),
            "last_subscribers": audience.get("last_subscribers"),
            "observed_delta": audience.get("observed_delta"),
        }
    return {"known_metric_mapping": False}


def _status(action_class: str, code: str) -> str:
    if action_class == "AUTO":
        return "data_collection_only"
    if action_class == "PR_REVIEW" and code in KNOWN_PR_REVIEW_CODES:
        return "proposal_ready"
    if action_class == "OWNER_APPROVAL":
        return "owner_approval_required"
    return "human_review_required"


def _issue_payload(
    *,
    city: str,
    report_date: date,
    code: str,
    message: str,
    fingerprint: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True, indent=2)
    return {
        "title": f"[Recommendation] {code} — {city} — {report_date.isoformat()}",
        "body": (
            "## Наблюдаемая рекомендация\n\n"
            f"{message}\n\n"
            "## Атрибуция\n\n"
            f"Fingerprint: `{fingerprint}`\n\n"
            "```json\n"
            f"{evidence_json}\n"
            "```\n\n"
            "## Ограничения\n\n"
            "- Данные наблюдательные и не доказывают причинность.\n"
            "- Этот issue не меняет редакционные правила автоматически.\n"
            "- Retained CPL приводится только при реально записанных spend и retention; "
            "валюты не конвертируются и не смешиваются.\n"
            "- Любое изменение кода/правил должно идти отдельным PR с проверками.\n"
        ),
    }


def prepare_recommendation_proposals(
    session: Session,
    *,
    report_date: date,
    city: str,
) -> list[RecommendationProposal]:
    """Persist stable proposals derived only from one stored DailyReport."""
    report_row = session.get(DailyReport, (report_date, city))
    if report_row is None:
        raise ValueError("daily report does not exist")
    report = report_row.payload
    if report.get("report_date") != report_date.isoformat() or report.get("city") != city:
        raise ValueError("daily report identity does not match its database key")

    recommendations = report.get("recommendations")
    if not isinstance(recommendations, list):
        raise ValueError("daily report recommendations must be a list")
    acquisition = _mapping(report.get("acquisition"), "acquisition")
    cpl = _retained_cpl_evidence(acquisition)
    limitations = report.get("limitations")
    if not isinstance(limitations, list) or not all(isinstance(value, str) for value in limitations):
        raise ValueError("daily report limitations must be a string list")

    result: list[RecommendationProposal] = []
    for raw in recommendations:
        if not isinstance(raw, dict):
            raise ValueError("daily report recommendation must be a mapping")
        action_class = raw.get("class")
        code = raw.get("code")
        message = raw.get("message")
        if not all(isinstance(value, str) and value.strip() for value in (action_class, code, message)):
            raise ValueError("recommendation class/code/message are required")
        assert isinstance(action_class, str)
        assert isinstance(code, str)
        assert isinstance(message, str)
        status = _status(action_class, code)
        evidence = {
            "report_date": report_date.isoformat(),
            "city": city,
            "recommendation": {
                "class": action_class,
                "code": code,
                "message": message,
            },
            "observed_metrics": _recommendation_metrics(code, report),
            "retained_cpl": cpl,
            "report_limitations": limitations,
            "causal_claim": False,
            "automatic_rule_change": False,
        }
        fingerprint = _canonical_hash(evidence)
        issue_payload = (
            _issue_payload(
                city=city,
                report_date=report_date,
                code=code,
                message=message,
                fingerprint=fingerprint,
                evidence=evidence,
            )
            if status in {"proposal_ready", "owner_approval_required"}
            else None
        )
        existing = session.scalar(
            select(RecommendationProposal).where(
                RecommendationProposal.fingerprint == fingerprint
            )
        )
        if existing is not None:
            result.append(existing)
            continue
        proposal = RecommendationProposal(
            city=city,
            report_date=report_date,
            recommendation_code=code,
            action_class=action_class,
            status=status,
            fingerprint=fingerprint,
            evidence=evidence,
            github_issue_payload=issue_payload,
        )
        session.add(proposal)
        session.flush()
        result.append(proposal)
    return result


def link_recommendation_target(
    session: Session,
    *,
    proposal_id: int,
    target_kind: str,
    target_repo: str,
    target_number: int,
    idempotency_key: str,
    linked_at: datetime | None = None,
) -> RecommendationTargetLink:
    """Append an attribution link after an external GitHub issue/PR was actually created."""
    kind = target_kind.strip().casefold()
    repo = target_repo.strip()
    key = idempotency_key.strip()
    if kind not in ALLOWED_TARGET_KINDS:
        raise ValueError("target_kind must be issue or pr")
    if not repo or "/" not in repo or repo.startswith("/") or repo.endswith("/"):
        raise ValueError("target_repo must be owner/name")
    if target_number <= 0:
        raise ValueError("target_number must be positive")
    if not key or len(key) > 128:
        raise ValueError("valid idempotency_key is required")
    proposal = session.get(RecommendationProposal, proposal_id)
    if proposal is None:
        raise ValueError("recommendation proposal does not exist")
    if proposal.status not in {"proposal_ready", "owner_approval_required"}:
        raise ValueError("recommendation proposal is not eligible for a GitHub target")

    existing = session.scalar(
        select(RecommendationTargetLink).where(
            RecommendationTargetLink.idempotency_key == key
        )
    )
    if existing is not None:
        if (
            existing.proposal_id != proposal_id
            or existing.target_kind != kind
            or existing.target_repo != repo
            or existing.target_number != target_number
        ):
            raise ValueError("idempotency_key already belongs to another recommendation link")
        return existing

    row = RecommendationTargetLink(
        proposal_id=proposal_id,
        target_kind=kind,
        target_repo=repo,
        target_number=target_number,
        idempotency_key=key,
        linked_at=linked_at or datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def build_recommendation_control(
    session: Session,
    *,
    city: str = "belgrade",
    limit: int = 100,
) -> dict[str, Any]:
    if limit < 1:
        raise ValueError("limit must be positive")
    proposals = list(
        session.scalars(
            select(RecommendationProposal)
            .where(RecommendationProposal.city == city)
            .order_by(
                RecommendationProposal.report_date.desc(),
                RecommendationProposal.id.desc(),
            )
            .limit(limit)
        )
    )
    ids = [proposal.id for proposal in proposals]
    links_by_proposal: dict[int, list[RecommendationTargetLink]] = {}
    if ids:
        for link in session.scalars(
            select(RecommendationTargetLink)
            .where(RecommendationTargetLink.proposal_id.in_(ids))
            .order_by(RecommendationTargetLink.linked_at, RecommendationTargetLink.id)
        ):
            links_by_proposal.setdefault(link.proposal_id, []).append(link)

    return {
        "version": 1,
        "city": city,
        "proposals": [
            {
                "proposal_id": proposal.id,
                "report_date": proposal.report_date.isoformat(),
                "recommendation_code": proposal.recommendation_code,
                "action_class": proposal.action_class,
                "status": proposal.status,
                "fingerprint": proposal.fingerprint,
                "github_issue_payload": proposal.github_issue_payload,
                "targets": [
                    {
                        "kind": link.target_kind,
                        "repo": link.target_repo,
                        "number": link.target_number,
                        "linked_at": link.linked_at.isoformat(),
                    }
                    for link in links_by_proposal.get(proposal.id, [])
                ],
            }
            for proposal in proposals
        ],
        "safety": {
            "automatic_github_creation": False,
            "automatic_pr_merge": False,
            "automatic_rule_change": False,
            "causal_claims_allowed": False,
            "synthetic_retained_cpl_allowed": False,
        },
    }
