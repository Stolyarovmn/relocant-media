import argparse
import asyncio
import json
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import SQLiteSession

from relocant_media.config import Settings
from relocant_media.ops.telegram_bootstrap import (
    TelegramBootstrapError,
    TelegramStagingBootstrapper,
    TopologyNames,
    default_names,
    env_payload,
    plan_payload,
    read_state,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Owner-only Telegram staging topology bootstrap")
    parser.add_argument("--city", default="Belgrade")
    parser.add_argument("--channel-title")
    parser.add_argument("--discussion-title")
    parser.add_argument("--forum-title")
    parser.add_argument("--state-path", type=Path)

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("plan", help="Show the requested Telegram topology without side effects")

    apply_parser = subparsers.add_parser(
        "apply", help="Create/resume the Telegram staging topology"
    )
    apply_parser.add_argument(
        "--confirm-create-staging",
        action="store_true",
        help="Required acknowledgement that this command creates Telegram resources",
    )

    subparsers.add_parser("check", help="Verify the created topology and print env mappings")
    return parser


def _names(args: argparse.Namespace) -> TopologyNames:
    defaults = default_names(args.city)
    return TopologyNames(
        channel=args.channel_title or defaults.channel,
        discussion=args.discussion_title or defaults.discussion,
        forum=args.forum_title or defaults.forum,
    )


def _state_path(args: argparse.Namespace, config: Settings) -> Path:
    return args.state_path or Path(config.telegram_bootstrap_state_path)


def _credential_errors(config: Settings) -> list[str]:
    errors: list[str] = []
    if config.telegram_api_id <= 0:
        errors.append("TELEGRAM_API_ID is not configured")
    if not config.telegram_api_hash:
        errors.append("TELEGRAM_API_HASH is not configured")
    if not config.telegram_mtproto_session_path:
        errors.append("TELEGRAM_MTPROTO_SESSION_PATH is not configured")
    return errors


async def _open_authorized_client(config: Settings) -> TelegramClient:
    errors = _credential_errors(config)
    if errors:
        raise TelegramBootstrapError("; ".join(errors))

    session_path = Path(config.telegram_mtproto_session_path)
    if not session_path.exists():
        raise TelegramBootstrapError(
            f"Telegram session is missing at {session_path}; run relocant-mtproto login first"
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
        raise TelegramBootstrapError(
            "Telegram MTProto session is not authorized; run relocant-mtproto login first"
        )
    return client


async def _apply(args: argparse.Namespace, config: Settings) -> dict[str, object]:
    if config.app_env.lower() == "prod":
        raise TelegramBootstrapError("refusing Telegram staging bootstrap with APP_ENV=prod")
    if not args.confirm_create_staging:
        raise TelegramBootstrapError("apply requires --confirm-create-staging")

    client = await _open_authorized_client(config)
    try:
        bootstrapper = TelegramStagingBootstrapper(client)
        state = await bootstrapper.apply(_names(args), _state_path(args, config))
        check = await bootstrapper.check(state)
    finally:
        await client.disconnect()

    return {
        "ok": bool(check.get("ok")),
        "status": state.status,
        "state_path": str(_state_path(args, config)),
        "env": env_payload(state),
        "check": check,
        "botfather": "manual_owner_step",
    }


async def _check(args: argparse.Namespace, config: Settings) -> dict[str, object]:
    state = read_state(_state_path(args, config))
    client = await _open_authorized_client(config)
    try:
        result = await TelegramStagingBootstrapper(client).check(state)
    finally:
        await client.disconnect()
    return result


def main() -> None:
    args = _parser().parse_args()
    config = Settings()
    state_path = _state_path(args, config)

    try:
        if args.command == "plan":
            result = plan_payload(_names(args), state_path)
        elif args.command == "apply":
            result = asyncio.run(_apply(args, config))
        elif args.command == "check":
            result = asyncio.run(_check(args, config))
        else:
            raise TelegramBootstrapError(f"unknown command: {args.command}")
    except TelegramBootstrapError as error:
        result = {"ok": False, "error": str(error)}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        raise SystemExit(2) from error

    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
