"""Ручное применение шапки и навигационного закрепа канала из репозитория."""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.services.channel_presentation import apply_presentation, load_presentation

CITIES = Path(__file__).resolve().parents[3] / "cities"


async def _run(city: str) -> dict[str, object]:
    folder = CITIES / city
    policy = json.loads((folder / "pin-policy.json").read_text(encoding="utf-8"))
    presentation = load_presentation(folder / "channel-presentation.yaml")
    async with httpx.AsyncClient(timeout=25, trust_env=False) as client:
        return await apply_presentation(
            TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"], client), policy, presentation)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", choices=["belgrade"], required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(_run(args.city)), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
