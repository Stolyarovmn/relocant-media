import json
import os
from dataclasses import asdict, dataclass, replace
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
    version: int = 1
    status: str = "creating"
    channel: PeerState | None = None
    discussion: PeerState | None = None
    discussion_history_open: bool = False
    discussion_linked: bool = False
    forum: PeerState | None = None


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


def _decode_peer(raw: object) -> PeerState | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise TypeError("peer state must be an object")
    return PeerState(
        peer_id=int(raw["peer_id"]),
        raw_id=int(raw["raw_id"]),
        title=str(raw["title"]),
        kind=str(raw["kind"]),
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
    if not isinstance(raw, dict):
        raise TelegramBootstrapError(f"invalid bootstrap state at {path}")
    try:
        return TopologyState(
            version=int(raw["version"]),
            status=str(raw["status"]),
            channel=_decode_peer(raw.get("channel")),
            discussion=_decode_peer(raw.get("discussion")),
            discussion_history_open=bool(raw.get("discussion_history_open", False)),
            discussion_linked=bool(raw.get("discussion_linked", False)),
            forum=_decode_peer(raw.get("forum")),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TelegramBootstrapError(f"invalid bootstrap state at {path}") from error


def env_payload(state: TopologyState) -> dict[str, str]:
    if state.channel is None or state.discussion is None or state.forum is None:
        raise TelegramBootstrapError("bootstrap topology is incomplete")
    return {
        "TELEGRAM_STAGING_CHANNEL_ID": str(state.channel.peer_id),
        "TELEGRAM_STAGING_DISCUSSION_GROUP_ID": str(state.discussion.peer_id),
        "TELEGRAM_STAGING_FORUM_GROUP_ID": str(state.forum.peer_id),
        "TELEGRAM_MTPROTO_CHANNEL_ID": str(state.channel.peer_id),
    }


def _is_expected_replay_error(error: Exception, allowed: set[str]) -> bool:
    return type(error).__name__ in allowed


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

    async def _entity(self, peer: PeerState) -> Any:
        return await self._client.get_entity(peer.peer_id)

    async def apply(self, names: TopologyNames, state_path: Path) -> TopologyState:
        state = read_state(state_path) if state_path.exists() else TopologyState()
        if state.status == "ready":
            return state

        if state.channel is None:
            channel = await self._create(
                title=names.channel,
                about="Private staging channel for Relocant Media. Not for production publishing.",
                broadcast=True,
            )
            state = replace(state, channel=_peer_state(channel, "broadcast_channel"))
            write_state(state_path, state)

        if state.discussion is None:
            discussion = await self._create(
                title=names.discussion,
                about="Staging discussion group linked to the Relocant Media staging channel.",
                megagroup=True,
            )
            state = replace(
                state,
                discussion=_peer_state(discussion, "discussion_supergroup"),
            )
            write_state(state_path, state)

        if not state.discussion_history_open:
            if state.discussion is None:
                raise TelegramBootstrapError("discussion group was not created")
            discussion = await self._entity(state.discussion)
            try:
                await self._client(
                    functions.channels.TogglePreHistoryHiddenRequest(
                        channel=_input_channel(discussion),
                        enabled=False,
                    )
                )
            except Exception as error:
                if not _is_expected_replay_error(error, {"ChatNotModifiedError"}):
                    raise
            state = replace(state, discussion_history_open=True)
            write_state(state_path, state)

        if not state.discussion_linked:
            if state.channel is None or state.discussion is None:
                raise TelegramBootstrapError("channel/discussion topology is incomplete")
            channel = await self._entity(state.channel)
            discussion = await self._entity(state.discussion)
            try:
                await self._client(
                    functions.channels.SetDiscussionGroupRequest(
                        broadcast=_input_channel(channel),
                        group=_input_channel(discussion),
                    )
                )
            except Exception as error:
                if not _is_expected_replay_error(error, {"LinkNotModifiedError"}):
                    raise
            state = replace(state, discussion_linked=True)
            write_state(state_path, state)

        if state.forum is None:
            forum = await self._create(
                title=names.forum,
                about="Separate staging forum for Relocant Media workflows and testing.",
                megagroup=True,
                forum=True,
            )
            state = replace(state, forum=_peer_state(forum, "forum_supergroup"))
            write_state(state_path, state)

        state = replace(state, status="ready")
        write_state(state_path, state)
        return state

    async def check(self, state: TopologyState) -> dict[str, object]:
        if state.channel is None or state.discussion is None or state.forum is None:
            raise TelegramBootstrapError("bootstrap topology is incomplete")

        channel = await self._entity(state.channel)
        discussion = await self._entity(state.discussion)
        forum = await self._entity(state.forum)
        channel_full = await self._client(
            functions.channels.GetFullChannelRequest(channel=_input_channel(channel))
        )
        linked_chat_id = getattr(channel_full.full_chat, "linked_chat_id", None)

        ok = (
            bool(getattr(channel, "broadcast", False))
            and bool(getattr(discussion, "megagroup", False))
            and bool(getattr(forum, "megagroup", False))
            and bool(getattr(forum, "forum", False))
            and linked_chat_id is not None
        )
        return {
            "ok": ok,
            "status": state.status,
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
            "env": env_payload(state),
        }
