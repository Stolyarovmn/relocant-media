from relocant_media.storage.models import Base


def test_expected_tables_are_registered() -> None:
    assert set(Base.metadata.tables) == {
        "source",
        "raw_item",
        "collector_failure",
        "post",
        "post_snapshot",
        "submission",
        "moderation_action",
        "ad_request",
        "daily_report",
    }
