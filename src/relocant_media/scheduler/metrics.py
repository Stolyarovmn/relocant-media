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


class PostMetricsProvider(Protocol):
    async def fetch_post_metrics(self, post: Post) -> PostMetrics: ...


class UnavailablePostMetricsProvider:
    async def fetch_post_metrics(self, post: Post) -> PostMetrics:
        message_id = post.telegram_message_id
        raise MetricsProviderUnavailable(
            f"post metrics provider is not configured for Telegram message {message_id}"
        )
