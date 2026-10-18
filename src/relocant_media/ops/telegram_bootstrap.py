import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from telethon import functions, types, utils


class TelegramBootstrapError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TopologyNames:
    channel: str
    discussion: str
    forum: str


@dataclass(frozen=True, slots=True)
class PeerState:
    peer_id: int
    raw_id: int
    title: str
    kind: str


@dataclass(frozen=True, slots=True)
class TopologyState:
    version: int
    channel: PeerState
    discussion: PeerState
    forum: PeerState


def default_names(city: str) -> TopologyNames:
    label = city.strip().title() or "Belgrade"
    return TopologyNames(
        channel=f"Relocant Media — {label} [STAGING]",
        discussion=f"Relocant Media — {label} Comments [STAGING]",
        forum=f"Relocant Media — {label} Forum [STAGING]",
    )


def plan_payload(names: TopologyNames, state_path: Path) -> dict[str, object]:
    return {
        "mode": "plan",
        "state_path": str(state_path),
        "actions": [
            {"action": "create_broadcast_channel", "title": names.channel},
            {"action": "create_discussion_supergroup", "title": names.discussion},
            {"action": "enable_discussion_history", "title": names.discussion},
            {"action": "link_discussion_group", "title": names.discussion},
            {"action": "create_forum_supergroup", "title": names.forum},
        ],
        "botfather": "manual_owner_step",
    }


def _input_channel(entity: Any) -> types.InputChannel:
    raw_id = getattr(entity, "id", None)
    access_hash = getattr(entity, "access_hash", None)
    if not isinstance(raw_id, int) or not isinstance(access_hash, int):
        raise TelegramBootstrapError("Telegram did not return a resolvable channel entity")
    return types.InputChannel(channel_id=raw_id, access_hash=access_hash)


def _created_channel(updates: Any) -> Any:
    chats = getattr(updates, "chats", None) or []
    for chat in chats:
        if getattr(chat, "id", None) is not None and getattr(chat, "access_hash", None) is not None:
            return chat
    raise TelegramBootstrapError("Telegram createChannel response contained no channel entity")


def _peer_state(entity: Any, kind: str) -> PeerState:
    raw_id = getattr(entity, "id", None)
    title = getattr(entity, "title", None)
    if not isinstance(raw_id, int) or not isinstance(title, str):
        raise TelegramBootstrapError("Telegram returned an incomplete channel entity")
    return PeerState(
        peer_id=int(utils.get_peer_id(entity)),
        raw_id=raw_id,
        title=title,
        kind=kind,
    )


def write_state(path: Path, state: TopologyState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def read_state(path: Path) -> TopologyState:
    if not path.exists():
        raise TelegramBootstrapError(f"bootstrap state does not exist at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    try:
        return TopologyState(
            version=int(raw["version"]),
            channel=PeerState(**raw["channel"]),
            discussion=PeerState(**raw["discussion"]),
            forum=PeerState(**raw["forum"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TelegramBootstrapError(f"invalid bootstrap state at {path}") from error


class TelegramStagingBootstrapper:
    """Owner-only one-shot topology creation; never imported by runtime schedulers."""

    def __init__(self, client: Any) -> None:
        self._client = client

    async def _create(
        self,
        *,
        title: str,
        about: str,
        broadcast: bool = False,
        megagroup: bool = False,
        forum: bool = False,
    ) -> Any:
        updates = await self._client(
            functions.channels.CreateChannelRequest(
                title=title,
                about=about,
                broadcast=broadcast,
                megagroup=megagroup,
                forum=forum,
            )
        )
        return _created_channel(updates)

    async def apply(self, names: TopologyNames, state_path: Path) -> TopologyState:
        if state_path.exists():
            raise TelegramBootstrapError(
                f"bootstrap state already exists at {state_path}; refusing duplicate topology"
            )

        channel = await self._create(
            title=names.channel,
            about="Private staging channel for Relocant Media. Not for production publishing.",
            broadcast=True,
        )
        discussion = await self._create(
            title=names.discussion,
            about="Staging discussion group linked to the Relocant Media staging channel.",
            megagroup=True,
        )
        discussion_input = _input_channel(discussion)
        await self._client(
            functions.channels.TogglePreHistoryHiddenRequest(
                channel=discussion_input,
                enabled=False,
            )
        )
        await self._client(
            functions.channels.SetDiscussionGroupRequest(
                broadcast=_input_channel(channel),
                group=discussion_input,
            )
        )
        forum = await self._create(
            title=names.forum,
            about="Separate staging forum for Relocant Media workflows and testing.",
            megagroup=True,
            forum=True,
        )

        state = TopologyState(
            version=1,
            channel=_peer_state(channel, "broadcast_channel"),
            discussion=_peer_state(discussion, "discussion_supergroup"),
            forum=_peer_state(forum, "forum_supergroup"),
        )
        write_state(state_path, state)
        return state

    async def check(self, state: TopologyState) -> dict[str, object]:
        channel = await self._client.get_entity(state.channel.peer_id)
        discussion = await self._client.get_entity(state.discussion.peer_id)
        forum = await self._client.get_entity(state.forum.peer_id)

        channel_full = await self._client(
            functions.channels.GetFullChannelRequest(channel=_input_channel(channel))
        )
        linked_chat_id = getattr(channel_full.full_chat, "linked_chat_id", None)

        return {
            "ok": True,
            "channel": {
                "peer_id": state.channel.peer_id,
                "title": getattr(channel, "title", None),
                "broadcast": bool(getattr(channel, "broadcast", False)),
                "discussion_linked": linked_chat_id is not None,
            },
            "discussion": {
                "peer_id": state.discussion.peer_id,
                "title": getattr(discussion, "title", None),
                "megagroup": bool(getattr(discussion, "megagroup", False)),
            },
            "forum": {
                "peer_id": state.forum.peer_id,
                "title": getattr(forum, "title", None),
                "megagroup": bool(getattr(forum, "megagroup", False)),
                "forum": bool(getattr(forum, "forum", False)),
            },
        }
