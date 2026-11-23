"""Проверяет описание канала и верхний служебный закреп Белграда."""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
import yaml

from relocant_media.bot.api import TelegramAPIError, TelegramBotAPI
from relocant_media.services.channel_presentation import ensure_service_pin_map_button

BELGRADE = Path("cities/belgrade")


async def run(*, apply: bool) -> dict[str, object]:
    policy = json.loads((BELGRADE / "pin-policy.json").read_text(encoding="utf-8"))
    config_file = BELGRADE / "channel-presentation.yaml"
    presentation = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
        bot = TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], client)
        return await ensure_service_pin_map_button(
            bot, policy, presentation, apply=apply, update_description=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(apply=args.apply))
    except (
        httpx.HTTPError, TelegramAPIError, ValueError, OSError, KeyError, yaml.YAMLError,
    ) as error:
        result = {"owner_action_required": True, "error_type": type(error).__name__}
    print(json.dumps(result, ensure_ascii=False))
    return int(bool(result["owner_action_required"]))


if __name__ == "__main__":
    raise SystemExit(main())
