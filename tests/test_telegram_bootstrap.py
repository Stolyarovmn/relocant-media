import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telethon import functions

from relocant_media.ops import telegram_bootstrap as bootstrap_module
from relocant_media.ops.telegram_bootstrap import (
    TelegramStagingBootstrapper,
    default_names,
    env_payload,
    plan_payload,
    read_state,
)


class FakeTelegramClient:
    def __init__(self, *, fail_forum_once: bool = False) -> None:
        self.requests: list[str] = []
        self.entities: dict[int, Any] = {}
        self.linked = False
        self.fail_forum_once = fail_forum_once
        self._forum_failed = False
        self._next_id = 100

    def _peer_id(self, raw_id: int) -> int:
        return -(1_000_000_000_000 + raw_id)

    async def __call__(self, request: object) -> object:
        self.requests.append(type(request).__name__)
        if isinstance(request, functions.channels.CreateChannelRequest):
            if request.forum and self.fail_forum_once and not self._forum_failed:
                self._forum_failed = True
                raise RuntimeError("simulated forum create failure")
            self._next_id += 1
            entity = SimpleNamespace(
                id=self._next_id,
                access_hash=10_000 + self._next_id,
                title=request.title,
                broadcast=bool(request.broadcast),
                megagroup=bool(request.megagroup),
                forum=bool(request.forum),
            )
            self.entities[self._peer_id(entity.id)] = entity
            return SimpleNamespace(chats=[entity])
        if isinstance(request, functions.channels.TogglePreHistoryHiddenRequest):
            return SimpleNamespace()
        if isinstance(request, functions.channels.SetDiscussionGroupRequest):
            self.linked = True
            return True
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(
                full_chat=SimpleNamespace(linked_chat_id=102 if self.linked else None)
            )
        raise AssertionError(f"unexpected Telegram request: {type(request).__name__}")

    async def get_entity(self, peer_id: int) -> object:
        return self.entities[peer_id]


@pytest.fixture(autouse=True)
def patch_peer_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap_module.utils,
        "get_peer_id",
        lambda entity: -(1_000_000_000_000 + int(entity.id)),
    )


def test_plan_is_deterministic_and_side_effect_free(tmp_path: Path) -> None:
    names = default_names("Belgrade")
    payload = plan_payload(names, tmp_path / "topology.json")

    assert payload["mode"] == "plan"
    assert payload["botfather"] == "manual_owner_step"
    actions = payload["actions"]
    assert isinstance(actions, list)
    assert [item["action"] for item in actions] == [
        "create_broadcast_channel",
        "create_discussion_supergroup",
        "enable_discussion_history",
        "link_discussion_group",
        "create_forum_supergroup",
    ]


def test_apply_creates_expected_topology_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "topology.json"
    client = FakeTelegramClient()
    bootstrapper = TelegramStagingBootstrapper(client)
    names = default_names("Belgrade")

    first = asyncio.run(bootstrapper.apply(names, path))
    request_count = len(client.requests)
    second = asyncio.run(bootstrapper.apply(names, path))

    assert first.status == "ready"
    assert second == first
    assert len(client.requests) == request_count
    assert client.requests.count("CreateChannelRequest") == 3
    assert client.requests.count("TogglePreHistoryHiddenRequest") == 1
    assert client.requests.count("SetDiscussionGroupRequest") == 1

    state = read_state(path)
    assert state.status == "ready"
    assert state.channel is not None
    assert state.discussion is not None
    assert state.forum is not None
    assert state.discussion_history_open is True
    assert state.discussion_linked is True
    assert state.forum.kind == "forum_supergroup"
    assert env_payload(state)["TELEGRAM_MTPROTO_CHANNEL_ID"] == str(state.channel.peer_id)


def test_apply_resumes_after_partial_failure_without_duplicate_channels(tmp_path: Path) -> None:
    path = tmp_path / "topology.json"
    client = FakeTelegramClient(fail_forum_once=True)
    bootstrapper = TelegramStagingBootstrapper(client)
    names = default_names("Belgrade")

    with pytest.raises(RuntimeError, match="simulated forum create failure"):
        asyncio.run(bootstrapper.apply(names, path))

    partial = read_state(path)
    assert partial.status == "creating"
    assert partial.channel is not None
    assert partial.discussion is not None
    assert partial.discussion_linked is True
    assert partial.forum is None
    creates_before_resume = client.requests.count("CreateChannelRequest")

    ready = asyncio.run(bootstrapper.apply(names, path))

    assert ready.status == "ready"
    assert ready.forum is not None
    assert client.requests.count("CreateChannelRequest") == creates_before_resume + 1


def test_check_verifies_channel_discussion_and_forum(tmp_path: Path) -> None:
    path = tmp_path / "topology.json"
    client = FakeTelegramClient()
    bootstrapper = TelegramStagingBootstrapper(client)
    state = asyncio.run(bootstrapper.apply(default_names("Belgrade"), path))

    result = asyncio.run(bootstrapper.check(state))

    assert result["ok"] is True
    assert result["channel"]["broadcast"] is True
    assert result["channel"]["discussion_linked"] is True
    assert result["discussion"]["megagroup"] is True
    assert result["forum"]["forum"] is True
