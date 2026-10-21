from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.services.experiments import (
    ExperimentArm,
    choose_assignment,
    evaluate_experiment,
    mark_rollback,
    persist_assignment,
    validate_experiment,
)
from relocant_media.storage.models import (
    Base,
    EditorialExperiment,
    ExperimentAssignment,
    Post,
    PostSnapshot,
    RawItem,
    Source,
)


def _experiment(**updates):
    values = {
        "id": "headline-v1",
        "city": "belgrade",
        "status": "running",
        "scope": "presentation",
        "control_rule_version": "style-control-v1",
        "treatment_rule_version": "style-treatment-v1",
        "eligibility_start": datetime(2026, 10, 20, tzinfo=UTC),
        "eligibility_end": datetime(2026, 10, 30, tzinfo=UTC),
        "success_metric": "engagement_per_view_observed",
        "horizon_hours": 24,
        "minimum_samples_per_arm": 2,
        "noise_floor": Decimal("0.010000"),
        "max_single_post_view_share": Decimal("0.8000"),
    }
    values.update(updates)
    return EditorialExperiment(**values)


def _source() -> Source:
    return Source(
        id="official",
        city="belgrade",
        name="Official",
        url="https://example.test",
        source_type="official",
        trust=Decimal("1"),
    )


def test_assignment_is_deterministic_and_bounded_by_window():
    experiment = _experiment()
    observed = datetime(2026, 10, 21, tzinfo=UTC)
    first = choose_assignment(experiment, raw_item_id=42, observed_at=observed)
    second = choose_assignment(experiment, raw_item_id=42, observed_at=observed)
    assert first == second
    assert first is not None
    assert first.arm in {ExperimentArm.CONTROL, ExperimentArm.TREATMENT}
    assert choose_assignment(
        experiment,
        raw_item_id=42,
        observed_at=datetime(2026, 10, 19, tzinfo=UTC),
    ) is None


def test_experiment_scope_cannot_change_factual_or_risk_rules():
    with pytest.raises(ValueError, match="presentation/content style"):
        validate_experiment(_experiment(scope="factual"))
    with pytest.raises(ValueError, match="success metric"):
        validate_experiment(_experiment(success_metric="fact_accuracy"))


def test_assignment_requires_real_rule_provenance_and_is_immutable():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    observed = datetime(2026, 10, 21, tzinfo=UTC)
    with Session(engine) as session:
        source = _source()
        session.add(source)
        session.flush()

        raw = RawItem(
            source_id=source.id,
            external_id="1",
            url="https://example.test/1",
            title="Title",
            body="Body",
            published_at=observed,
        )
        experiment = _experiment()
        session.add_all([raw, experiment])
        session.flush()

        decision = choose_assignment(experiment, raw_item_id=raw.id, observed_at=observed)
        assert decision is not None
        post = Post(
            city="belgrade",
            category="events",
            status="ready",
            title="Пост",
            body="Текст",
            confidence=Decimal("0.9"),
            editorial_rule_version=decision.rule_version,
        )
        session.add(post)
        session.flush()

        with pytest.raises(ValueError, match="does not match experiment assignment"):
            persist_assignment(
                session,
                experiment=experiment,
                raw_item_id=raw.id,
                post_id=post.id,
                applied_rule_version="wrong-version",
                observed_at=observed,
            )

        assignment = persist_assignment(
            session,
            experiment=experiment,
            raw_item_id=raw.id,
            post_id=post.id,
            applied_rule_version=decision.rule_version,
            observed_at=observed,
        )
        assert assignment is not None
        assert assignment.arm == decision.arm.value
        assert persist_assignment(
            session,
            experiment=experiment,
            raw_item_id=raw.id,
            post_id=post.id,
            applied_rule_version=decision.rule_version,
            observed_at=observed + timedelta(minutes=1),
        ) is assignment
    engine.dispose()


def test_evaluation_is_read_only_observational_and_has_noise_floor():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime(2026, 10, 21, tzinfo=UTC)
    with Session(engine) as session:
        source = _source()
        session.add(source)
        session.flush()

        experiment = _experiment()
        session.add(experiment)
        session.flush()
        rows = [
            (1, "control", "style-control-v1", 100, 10),
            (2, "control", "style-control-v1", 100, 12),
            (3, "treatment", "style-treatment-v1", 100, 14),
            (4, "treatment", "style-treatment-v1", 100, 16),
        ]
        for external_id, arm, rule_version, views, interactions in rows:
            raw = RawItem(
                source_id=source.id,
                external_id=str(external_id),
                url=f"https://example.test/{external_id}",
                title=f"Source {external_id}",
                body="Body",
                published_at=now,
            )
            session.add(raw)
            session.flush()

            post = Post(
                city="belgrade",
                category="events",
                status="published",
                title=f"Post {external_id}",
                body="Body",
                confidence=Decimal("0.9"),
                editorial_rule_version=rule_version,
            )
            session.add(post)
            session.flush()
            session.add(
                ExperimentAssignment(
                    experiment_id=experiment.id,
                    raw_item_id=raw.id,
                    post_id=post.id,
                    arm=arm,
                    rule_version=rule_version,
                    assigned_at=now,
                )
            )
            session.add(
                PostSnapshot(
                    post_id=post.id,
                    captured_at=now + timedelta(hours=24),
                    horizon_hours=24,
                    views=views,
                    reactions=interactions,
                    comments=0,
                    forwards=0,
                )
            )
        session.flush()
        before = experiment.status
        report = evaluate_experiment(session, experiment.id)
        assert report["observation"] == "treatment_higher_observed"
        assert report["decision_eligible"] is True
        assert report["automatic_winner"] is None
        assert report["observed_delta"] == pytest.approx(0.04)
        assert experiment.status == before

        experiment.noise_floor = Decimal("0.050000")
        session.flush()
        report = evaluate_experiment(session, experiment.id)
        assert report["observation"] == "within_noise_floor"
        assert report["automatic_winner"] is None
    engine.dispose()


def test_rollback_is_explicit_and_limited_to_registered_rules():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        experiment = _experiment()
        session.add(experiment)
        session.flush()
        with pytest.raises(ValueError, match="belong to the experiment"):
            mark_rollback(
                session,
                experiment_id=experiment.id,
                rollback_rule_version="unrelated-v1",
                marked_at=datetime(2026, 10, 21, tzinfo=UTC),
            )
        result = mark_rollback(
            session,
            experiment_id=experiment.id,
            rollback_rule_version=experiment.control_rule_version,
            marked_at=datetime(2026, 10, 21, tzinfo=UTC),
        )
        assert result.status == "rolled_back"
        assert result.rollback_rule_version == experiment.control_rule_version
        assert result.rollback_marked_at is not None
    engine.dispose()
