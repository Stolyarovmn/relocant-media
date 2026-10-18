import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

import pytest
from telethon import functions, types

from relocant_media.analytics.mtproto import MTProtoMetricsProvider
from relocant_media.scheduler.metrics import MetricsProviderUnavailable
from relocant_media.storage.models import Post


class FakeTelegramClient:
    def __init__(self, *, views: int | None = 120) -> None:
        self.views = views
        self.requests: list[str] = []

    async def __call__(self, request: object) -> object:
        self.requests.append(type(request).__name__)
        if isinstance(request, functions.channels.GetMessagesRequest):
            message = SimpleNamespace(
                views=self.views,
                forwards=9,
                reactions=SimpleNamespace(
                    results=[SimpleNamespace(count=4), SimpleNamespace(count=3)]
                ),
                replies=SimpleNamespace(replies=5),
            )
            return SimpleNamespace(messages=[message])
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(full_chat=SimpleNamespace(participants_count=4321))
        raise AssertionError(f"unexpected request: {type(request).__name__}")


def _post() -> Post:
    return Post(
        city="belgrade",
        category="transport",
        status="published",
        title="Test",
        body="Body",
        confidence=Decimal("0.9900"),
        telegram_message_id=77,
    )


def _provider(client: FakeTelegramClient) -> MTProtoMetricsProvider:
    provider = MTProtoMetricsProvider(cast(Any, client), "@test_channel")
    provider._input_channel = types.InputChannel(channel_id=123, access_hash=456)
    return provider


def test_post_metrics_use_non_incrementing_channel_message_read() -> None:
    client = FakeTelegramClient()
    metrics = asyncio.run(_provider(client).fetch_post_metrics(_post()))

    assert metrics.views == 120
    assert metrics.forwards == 9
    assert metrics.reactions == 7
    assert metrics.comments == 5
    assert client.requests == ["GetMessagesRequest"]


def test_channel_subscribers_are_observed_from_full_channel() -> None:
    client = FakeTelegramClient()
    metrics = asyncio.run(_provider(client).fetch_channel_metrics())

    assert metrics.subscribers == 4321
    assert metrics.source == "telegram_mtproto:channels.getFullChannel"
    assert client.requests == ["GetFullChannelRequest"]


def test_missing_view_counter_is_not_fabricated() -> None:
    client = FakeTelegramClient(views=None)
    with pytest.raises(MetricsProviderUnavailable, match="did not expose a view counter"):
        asyncio.run(_provider(client).fetch_post_metrics(_post()))
