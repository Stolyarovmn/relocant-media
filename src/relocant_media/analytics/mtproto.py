from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from telethon import TelegramClient, functions, types, utils
from telethon.errors import FloodWaitError
from telethon.sessions import SQLiteSession

from relocant_media.config import Settings, mtproto_setup_errors
from relocant_media.scheduler.metrics import (
    ChannelMetrics,
    MetricsProviderUnavailable,
    PostMetrics,
)
from relocant_media.storage.models import Post


def _peer_reference(value: str) -> str | int:
    normalized = value.strip()
    if not normalized:
        raise MetricsProviderUnavailable("Telegram MTProto channel is not configured")
    try:
        return int(normalized)
    except ValueError:
        return normalized


def _reaction_count(message: Any) -> int:
    reactions = getattr(message, "reactions", None)
    if reactions is None:
        return 0
    return sum(int(result.count) for result in reactions.results)


def _reply_count(message: Any) -> int:
    replies = getattr(message, "replies", None)
    if replies is None:
        return 0
    return int(replies.replies)


def _post_metrics_from_message(message: Any) -> PostMetrics:
    views = getattr(message, "views", None)
    if views is None:
        raise MetricsProviderUnavailable("Telegram did not expose a view counter for this post")
    return PostMetrics(
        views=int(views),
        reactions=_reaction_count(message),
        comments=_reply_count(message),
        forwards=int(getattr(message, "forwards", None) or 0),
    )


class MTProtoMetricsProvider:
    """Read-only analytics adapter for one Telegram channel.

    This class intentionally exposes only metric reads. It has no helpers for sending,
    editing, deleting, joining, inviting, moderation, DMs or advertising actions.
    """

    def __init__(self, client: TelegramClient, channel: str) -> None:
        self._client = client
        self._channel = _peer_reference(channel)
        self._input_channel: Any | None = None

    async def _resolve_channel(self) -> Any:
        if self._input_channel is not None:
            return self._input_channel
        entity = await self._client.get_entity(self._channel)
        input_peer = utils.get_input_peer(entity)
        self._input_channel = utils.get_input_channel(input_peer)
        return self._input_channel

    async def fetch_post_metrics(self, post: Post) -> PostMetrics:
        message_id = post.telegram_message_id
        if message_id is None:
            raise MetricsProviderUnavailable("post has no Telegram message id")
        try:
            channel = await self._resolve_channel()
            response = await self._client(
                functions.channels.GetMessagesRequest(
                    channel=channel,
                    id=[types.InputMessageID(id=int(message_id))],
                )
            )
        except FloodWaitError as error:
            raise MetricsProviderUnavailable(
                f"Telegram flood wait: retry after {int(error.seconds)} seconds"
            ) from error
        except (TypeError, ValueError) as error:
            raise MetricsProviderUnavailable(f"Telegram channel resolution failed: {error}") from error

        messages = getattr(response, "messages", [])
        if not messages or not isinstance(messages[0], types.Message):
            raise MetricsProviderUnavailable(f"Telegram message {message_id} is unavailable")
        return _post_metrics_from_message(messages[0])

    async def fetch_channel_metrics(self) -> ChannelMetrics:
        try:
            channel = await self._resolve_channel()
            response = await self._client(
                functions.channels.GetFullChannelRequest(channel=channel)
            )
        except FloodWaitError as error:
            raise MetricsProviderUnavailable(
                f"Telegram flood wait: retry after {int(error.seconds)} seconds"
            ) from error
        except (TypeError, ValueError) as error:
            raise MetricsProviderUnavailable(f"Telegram channel resolution failed: {error}") from error

        subscribers = getattr(response.full_chat, "participants_count", None)
        if subscribers is None:
            raise MetricsProviderUnavailable("Telegram did not expose the channel subscriber count")
        return ChannelMetrics(
            subscribers=int(subscribers),
            source="telegram_mtproto:channels.getFullChannel",
        )


async def _connected_client(config: Settings) -> TelegramClient:
    errors = mtproto_setup_errors(config)
    if errors:
        raise MetricsProviderUnavailable("; ".join(errors))

    session_path = Path(config.telegram_mtproto_session_path)
    if not session_path.exists():
        raise MetricsProviderUnavailable(
            f"Telegram MTProto session is missing at {session_path}; run relocant-mtproto login"
        )

    client = TelegramClient(
        SQLiteSession(str(session_path)),
        config.telegram_api_id,
        config.telegram_api_hash,
        receive_updates=False,
    )
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise MetricsProviderUnavailable(
            "Telegram MTProto session is not authorized; run relocant-mtproto login"
        )
    return client


@asynccontextmanager
async def open_mtproto_metrics_provider(
    config: Settings,
) -> AsyncIterator[MTProtoMetricsProvider]:
    client = await _connected_client(config)
    try:
        yield MTProtoMetricsProvider(client, config.telegram_mtproto_channel_id)
    finally:
        await client.disconnect()
