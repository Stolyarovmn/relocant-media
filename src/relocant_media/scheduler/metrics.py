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
    def fetch_post_metrics(self, post: Post) -> PostMetrics: ...


class UnavailablePostMetricsProvider:
    def fetch_post_metrics(self, post: Post) -> PostMetrics:
        raise MetricsProviderUnavailable(
            f"post metrics provider is not configured for Telegram message {post.telegram_message_id}"
        )
