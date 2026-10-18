from dataclasses import dataclass
from typing import Protocol

from relocant_media.storage.models import Post


class MetricsProviderUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PostMetrics:
    views: int
    reactions: int
    comments: int
    forwards: int
    subscriber_delta: int | None = None


@dataclass(frozen=True, slots=True)
class ChannelMetrics:
    subscribers: int
    source: str


class MetricsProvider(Protocol):
    async def fetch_post_metrics(self, post: Post) -> PostMetrics: ...

    async def fetch_channel_metrics(self) -> ChannelMetrics: ...


class UnavailableMetricsProvider:
    def __init__(self, reason: str = "Telegram analytics provider is not configured") -> None:
        self._reason = reason

    async def fetch_post_metrics(self, post: Post) -> PostMetrics:
        message_id = post.telegram_message_id
        raise MetricsProviderUnavailable(f"{self._reason}; Telegram message {message_id}")

    async def fetch_channel_metrics(self) -> ChannelMetrics:
        raise MetricsProviderUnavailable(self._reason)


# Backward-compatible names while callers migrate to the combined provider contract.
PostMetricsProvider = MetricsProvider
UnavailablePostMetricsProvider = UnavailableMetricsProvider
