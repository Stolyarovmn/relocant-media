from collections.abc import Mapping
from enum import StrEnum


class UpdateKind(StrEnum):
    MESSAGE = "message"
    CHANNEL_POST = "channel_post"
    EDITED_CHANNEL_POST = "edited_channel_post"
    MESSAGE_REACTION = "message_reaction"
    MESSAGE_REACTION_COUNT = "message_reaction_count"
    CHAT_MEMBER = "chat_member"
    MY_CHAT_MEMBER = "my_chat_member"
    CALLBACK_QUERY = "callback_query"
    UNKNOWN = "unknown"


SUPPORTED_UPDATE_KEYS: tuple[UpdateKind, ...] = (
    UpdateKind.MESSAGE,
    UpdateKind.CHANNEL_POST,
    UpdateKind.EDITED_CHANNEL_POST,
    UpdateKind.MESSAGE_REACTION,
    UpdateKind.MESSAGE_REACTION_COUNT,
    UpdateKind.CHAT_MEMBER,
    UpdateKind.MY_CHAT_MEMBER,
    UpdateKind.CALLBACK_QUERY,
)


def classify_update(payload: Mapping[str, object]) -> UpdateKind:
    for kind in SUPPORTED_UPDATE_KEYS:
        if kind.value in payload:
            return kind
    return UpdateKind.UNKNOWN


def update_id(payload: Mapping[str, object]) -> int | None:
    value = payload.get("update_id")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None
