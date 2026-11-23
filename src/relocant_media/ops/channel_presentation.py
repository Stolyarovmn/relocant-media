"""Обновляет описание канала Белграда или проверяет закреп карты."""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
import yaml

from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI
from relocant_media.services.channel_presentation import (
    apply_pin_texts,
    ensure_channel_description,
    ensure_map_pin_on_top,
)

BELGRADE = Path("cities/belgrade")


async def run(*, apply: bool, pin_map: bool = False,
              pin_existing: int | None = None, texts: bool = False) -> dict[str, object]:
    if pin_map and texts:
        raise ValueError("select one channel presentation operation")
    policy = json.loads((BELGRADE / "pin-policy.json").read_text(encoding="utf-8"))
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        bot = TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], client)
        if pin_map:
            # Отдельный ручной режим: единственный путь, который отправляет пост карты.
            return await ensure_map_pin_on_top(bot, policy, apply=apply,
                                               pin_existing=pin_existing)
        config_file = BELGRADE / "channel-presentation.yaml"
        presentation = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        if texts:
            return await apply_pin_texts(bot, policy, presentation, apply=apply)
        return await ensure_channel_description(bot, policy, presentation, apply=apply)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--pin-map", action="store_true")
    parser.add_argument("--texts", action="store_true", help="тексты служебных закрепов")
    parser.add_argument("--pin-existing", type=int, help="закрепить уже отправленный пост карты")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(apply=args.apply, pin_map=args.pin_map or bool(args.pin_existing),
                                 pin_existing=args.pin_existing, texts=args.texts))
    except (
        httpx.HTTPError, TelegramAPIError, ValueError, OSError, KeyError, yaml.YAMLError,
    ) as error:
        result = {"owner_action_required": True, "error_type": type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return int(bool(result["owner_action_required"]))


if __name__ == "__main__":
    raise SystemExit(main())
