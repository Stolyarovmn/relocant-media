"""Deterministic editorial experiment registry and read-only evaluation."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from statistics import fmean

from sqlalchemy import select
from sqlalchemy.orm import Session

from relocant_media.storage.models import (
    EditorialExperiment,
    ExperimentAssignment,
    Post,
    PostSnapshot,
)

SAFE_EXPERIMENT_SCOPES = frozenset({"presentation", "content_style"})
SUPPORTED_SUCCESS_METRICS = frozenset({"engagement_per_view_observed"})
VALID_EXPERIMENT_STATUSES = frozenset(
    {"draft", "running", "paused", "completed", "rolled_back"}
)


class ExperimentArm(StrEnum):
    CONTROL = "control"
    TREATMENT = "treatment"


@dataclass(frozen=True, slots=True)
class AssignmentDecision:
    experiment_id: str
    arm: ExperimentArm
    rule_version: str


@dataclass(frozen=True, slots=True)
class ArmObservation:
    samples: int
    mean_metric: float | None
    total_views: int
    max_single_post_view_share: float | None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


def validate_experiment(experiment: EditorialExperiment) -> None:
    if experiment.status not in VALID_EXPERIMENT_STATUSES:
        raise ValueError("unsupported experiment status")
    if experiment.scope not in SAFE_EXPERIMENT_SCOPES:
        raise ValueError("experiments may change presentation/content style only")
    if experiment.success_metric not in SUPPORTED_SUCCESS_METRICS:
        raise ValueError("unsupported experiment success metric")
    if experiment.control_rule_version == experiment.treatment_rule_version:
        raise ValueError("control and treatment rule versions must differ")
    if experiment.minimum_samples_per_arm < 1:
        raise ValueError("minimum_samples_per_arm must be positive")
    if experiment.horizon_hours < 1:
        raise ValueError("horizon_hours must be positive")
    noise_floor = float(experiment.noise_floor)
    if not 0 <= noise_floor <= 1:
        raise ValueError("noise_floor must be between 0 and 1")
    max_share = float(experiment.max_single_post_view_share)
    if not 0 < max_share <= 1:
        raise ValueError("max_single_post_view_share must be in (0, 1]")
    if experiment.eligibility_end is not None:
        if _utc(experiment.eligibility_end) <= _utc(experiment.eligibility_start):
            raise ValueError("eligibility_end must be after eligibility_start")


def choose_assignment(
    experiment: EditorialExperiment,
    *,
    raw_item_id: int,
    observed_at: datetime,
) -> AssignmentDecision | None:
    """Choose an arm reproducibly without changing any editorial state."""
    validate_experiment(experiment)
    if experiment.status != "running":
        return None
    observed = _utc(observed_at)
    if observed < _utc(experiment.eligibility_start):
        return None
    if experiment.eligibility_end is not None:
        if observed >= _utc(experiment.eligibility_end):
            return None

    digest = hashlib.sha256(f"{experiment.id}:{raw_item_id}".encode()).digest()
    arm = ExperimentArm.CONTROL if digest[0] % 2 == 0 else ExperimentArm.TREATMENT
    rule_version = (
        experiment.control_rule_version
        if arm is ExperimentArm.CONTROL
        else experiment.treatment_rule_version
    )
    return AssignmentDecision(experiment.id, arm, rule_version)


def persist_assignment(
    session: Session,
    *,
    experiment: EditorialExperiment,
    raw_item_id: int,
    post_id: int,
    applied_rule_version: str,
    observed_at: datetime,
) -> ExperimentAssignment | None:
    """Persist provenance only when the actually applied rule matches the chosen arm."""
    decision = choose_assignment(
        experiment,
        raw_item_id=raw_item_id,
        observed_at=observed_at,
    )
    if decision is None:
        return None
    if applied_rule_version != decision.rule_version:
        raise ValueError("applied editorial rule does not match experiment assignment")

    post = session.get(Post, post_id)
    if post is None:
        raise ValueError("experiment assignment requires an existing post")
    if post.editorial_rule_version != applied_rule_version:
        raise ValueError("post rule provenance does not match applied rule")

    existing = session.get(ExperimentAssignment, (experiment.id, raw_item_id))
    if existing is not None:
        if (
            existing.post_id != post_id
            or existing.arm != decision.arm.value
            or existing.rule_version != decision.rule_version
        ):
            raise ValueError("experiment assignment is immutable")
        return existing

    assignment = ExperimentAssignment(
        experiment_id=experiment.id,
        raw_item_id=raw_item_id,
        post_id=post_id,
        arm=decision.arm.value,
        rule_version=decision.rule_version,
        assigned_at=_utc(observed_at),
    )
    session.add(assignment)
    session.flush()
    return assignment


def _latest_snapshot(
    session: Session,
    *,
    post_id: int,
    horizon_hours: int,
) -> PostSnapshot | None:
    return session.scalar(
        select(PostSnapshot)
        .where(
            PostSnapshot.post_id == post_id,
            PostSnapshot.horizon_hours == horizon_hours,
        )
        .order_by(PostSnapshot.captured_at.desc())
        .limit(1)
    )


def _arm_observation(
    session: Session,
    assignments: list[ExperimentAssignment],
    *,
    horizon_hours: int,
) -> ArmObservation:
    values: list[float] = []
    views: list[int] = []
    for assignment in assignments:
        if assignment.post_id is None:
            continue
        snapshot = _latest_snapshot(
            session,
            post_id=assignment.post_id,
            horizon_hours=horizon_hours,
        )
        if snapshot is None or snapshot.views <= 0:
            continue
        interactions = snapshot.reactions + snapshot.comments + snapshot.forwards
        values.append(interactions / snapshot.views)
        views.append(snapshot.views)

    total_views = sum(views)
    max_share = max(views) / total_views if views and total_views else None
    return ArmObservation(
        samples=len(values),
        mean_metric=fmean(values) if values else None,
        total_views=total_views,
        max_single_post_view_share=max_share,
    )


def evaluate_experiment(session: Session, experiment_id: str) -> dict[str, object]:
    """Describe observed results; never mutate rules or declare an automatic winner."""
    experiment = session.get(EditorialExperiment, experiment_id)
    if experiment is None:
        raise ValueError("experiment does not exist")
    validate_experiment(experiment)

    assignments = list(
        session.scalars(
            select(ExperimentAssignment).where(
                ExperimentAssignment.experiment_id == experiment_id
            )
        )
    )
    control = _arm_observation(
        session,
        [item for item in assignments if item.arm == ExperimentArm.CONTROL.value],
        horizon_hours=experiment.horizon_hours,
    )
    treatment = _arm_observation(
        session,
        [item for item in assignments if item.arm == ExperimentArm.TREATMENT.value],
        horizon_hours=experiment.horizon_hours,
    )

    enough_samples = (
        control.samples >= experiment.minimum_samples_per_arm
        and treatment.samples >= experiment.minimum_samples_per_arm
    )
    concentration_ok = all(
        observation.max_single_post_view_share is None
        or observation.max_single_post_view_share
        <= float(experiment.max_single_post_view_share)
        for observation in (control, treatment)
    )

    delta: float | None = None
    direction = "insufficient_samples"
    if enough_samples and not concentration_ok:
        direction = "concentration_guard"
    elif enough_samples and control.mean_metric is not None and treatment.mean_metric is not None:
        delta = treatment.mean_metric - control.mean_metric
        if abs(delta) < float(experiment.noise_floor):
            direction = "within_noise_floor"
        elif delta > 0:
            direction = "treatment_higher_observed"
        else:
            direction = "control_higher_observed"

    def payload(observation: ArmObservation) -> dict[str, object]:
        return {
            "samples": observation.samples,
            "mean_metric": observation.mean_metric,
            "total_views": observation.total_views,
            "max_single_post_view_share": observation.max_single_post_view_share,
        }

    return {
        "experiment_id": experiment.id,
        "status": experiment.status,
        "scope": experiment.scope,
        "success_metric": experiment.success_metric,
        "horizon_hours": experiment.horizon_hours,
        "minimum_samples_per_arm": experiment.minimum_samples_per_arm,
        "noise_floor": float(experiment.noise_floor),
        "control": payload(control),
        "treatment": payload(treatment),
        "observed_delta": delta,
        "observation": direction,
        "decision_eligible": enough_samples and concentration_ok,
        "automatic_winner": None,
        "limitations": [
            "Observational output does not prove causality.",
            "No LLM or evaluator may promote a treatment automatically.",
            "Factual, source-risk and safety rules are outside experiment scope.",
        ],
    }


def mark_rollback(
    session: Session,
    *,
    experiment_id: str,
    rollback_rule_version: str,
    marked_at: datetime,
) -> EditorialExperiment:
    """Record an explicit rollback marker; evaluation never calls this automatically."""
    experiment = session.get(EditorialExperiment, experiment_id)
    if experiment is None:
        raise ValueError("experiment does not exist")
    validate_experiment(experiment)
    if rollback_rule_version not in {
        experiment.control_rule_version,
        experiment.treatment_rule_version,
    }:
        raise ValueError("rollback rule must belong to the experiment")
    experiment.status = "rolled_back"
    experiment.rollback_rule_version = rollback_rule_version
    experiment.rollback_marked_at = _utc(marked_at)
    session.flush()
    return experiment
