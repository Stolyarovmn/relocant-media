from relocant_media.storage.models import Base


def test_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "source",
        "raw_item",
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
    }
