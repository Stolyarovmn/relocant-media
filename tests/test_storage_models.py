from relocant_media.scheduler.models import SchedulerLease, SchedulerRun  # noqa: F401
from relocant_media.storage import ad_review as ad_review_models  # noqa: F401
from relocant_media.storage import correction_control as correction_control_models  # noqa: F401
from relocant_media.storage import media_models  # noqa: F401
from relocant_media.storage import revision_control as revision_control_models  # noqa: F401
from relocant_media.storage.models import Base


def test_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "source_revision",
        "source_revision_decision",
        "source_revision_materialization",
        "reader_correction_decision",
        "suggested_post_review",
        "source",
        "editorial_run",
        "editorial_experiment",
        "experiment_assignment",
        "raw_item",
        "raw_item_media_snapshot",
        "collector_failure",
        "post",
        "post_snapshot",
        "channel_snapshot",
        "acquisition_cohort",
        "ai_usage_event",
        "submission",
        "moderation_action",
        "telegram_update_receipt",
        "telegram_conversation",
        "ad_request",
        "daily_report",
        "scheduler_lease",
        "scheduler_run",
    }
