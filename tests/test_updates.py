from relocant_media.bot.updates import UpdateKind, classify_update, update_id


def test_classify_channel_post() -> None:
    payload: dict[str, object] = {"update_id": 42, "channel_post": {"message_id": 7}}
    assert classify_update(payload) is UpdateKind.CHANNEL_POST
    assert update_id(payload) == 42


def test_unknown_update_and_invalid_id() -> None:
    payload: dict[str, object] = {"update_id": True, "business_connection": {}}
    assert classify_update(payload) is UpdateKind.UNKNOWN
    assert update_id(payload) is None
