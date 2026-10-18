import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import SQLiteSession

from relocant_media.analytics.mtproto import MTProtoMetricsProvider
from relocant_media.config import Settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Owner-only Telegram MTProto analytics setup")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("login", help="Authorize/update the local analytics session")
    subparsers.add_parser("check", help="Verify the authorized account and analytics channel")
    return parser


def _credential_errors(config: Settings) -> list[str]:
    errors: list[str] = []
    if config.telegram_api_id <= 0:
        errors.append("TELEGRAM_API_ID is not configured")
    if not config.telegram_api_hash:
        errors.append("TELEGRAM_API_HASH is not configured")
    if not config.telegram_mtproto_session_path:
        errors.append("TELEGRAM_MTPROTO_SESSION_PATH is not configured")
    return errors


def _session_path(config: Settings) -> Path:
    path = Path(config.telegram_mtproto_session_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _secure_session_file(path: Path) -> None:
    if path.exists():
        os.chmod(path, 0o600)


async def _login(config: Settings) -> dict[str, object]:
    errors = _credential_errors(config)
    if errors:
        raise RuntimeError("; ".join(errors))

    path = _session_path(config)
    client = TelegramClient(
        SQLiteSession(str(path)),
        config.telegram_api_id,
        config.telegram_api_hash,
        receive_updates=False,
    )
    try:
        await client.start(
            phone=lambda: getpass.getpass("Telegram phone number: ").strip(),
            code_callback=lambda: getpass.getpass("Telegram login code: ").strip(),
            password=lambda: getpass.getpass("Telegram 2FA password: "),
        )
        me = await client.get_me()
    finally:
        await client.disconnect()
        _secure_session_file(path)

    return {
        "ok": True,
        "authorized": True,
        "user_id": int(me.id),
        "username": getattr(me, "username", None),
        "session_path": str(path),
    }


async def _check(config: Settings) -> dict[str, object]:
    errors = _credential_errors(config)
    if errors:
        raise RuntimeError("; ".join(errors))
    if not config.telegram_mtproto_channel_id:
        raise RuntimeError("TELEGRAM_MTPROTO_CHANNEL_ID is not configured")

    path = _session_path(config)
    if not path.exists():
        raise RuntimeError(f"session does not exist at {path}; run login first")

    client = TelegramClient(
        SQLiteSession(str(path)),
        config.telegram_api_id,
        config.telegram_api_hash,
        receive_updates=False,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("session is not authorized; run login again")
        me = await client.get_me()
        provider = MTProtoMetricsProvider(client, config.telegram_mtproto_channel_id)
        channel = await provider.fetch_channel_metrics()
    finally:
        await client.disconnect()
        _secure_session_file(path)

    return {
        "ok": True,
        "authorized": True,
        "user_id": int(me.id),
        "username": getattr(me, "username", None),
        "channel_subscribers": channel.subscribers,
        "source": channel.source,
    }


def main() -> None:
    args = _parser().parse_args()
    config = Settings()
    if args.command == "login":
        result = asyncio.run(_login(config))
    elif args.command == "check":
        result = asyncio.run(_check(config))
    else:
        raise RuntimeError(f"unknown command: {args.command}")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
