"""Читает текущий проверенный снимок и собирает утренний выпуск без отправки."""

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.bot.formatting import formatted_text_units
from relocant_media.collectors.rhmz_forecast import collect_weather
from relocant_media.collectors.tls import source_ssl_context
from relocant_media.ops.belgrade_digests import (
    _cover,
    _morning_continuations,
    _with_cover,
    due_slot,
    morning_entries,
    render,
)


async def preview(
    database: Path, as_of: datetime, telegram_app: str, *, state: dict[str, Any] | None = None,
) -> dict[str, object]:
    if as_of.utcoffset() is None:
        raise ValueError("preview requires a timestamp with offset")
    slot = due_slot(as_of)
    if slot is None or slot.kind != "morning":
        raise ValueError("choose a timestamp in the morning slot")
    async with httpx.AsyncClient(
        timeout=12, follow_redirects=False, verify=source_ssl_context(),
    ) as client:
        weather = await collect_weather(client, slot.scheduled)
    engine = create_engine(f"sqlite:///{database.resolve().as_uri()}?mode=ro&uri=true")
    try:
        with Session(engine) as session:
            entries = morning_entries(session, slot.scheduled, weather.forecast, weather.warning)
    finally:
        engine.dispose()
    if state is not None:
        entries = _morning_continuations(slot, entries, state)
    draft = render(slot, entries, telegram_app)
    draft, photo = _with_cover(slot, entries, draft, telegram_app) if draft else (None, None)
    cover = _cover(slot) if photo else None
    return {
        "scheduled": slot.scheduled.isoformat(),
        "weather_sources": weather.statuses(),
        "cover": str(photo) if photo else None,
        "cover_credit": cover[1] if cover else None,
        "caption_units": formatted_text_units(draft.html) if draft and photo else None,
        "entries": [{"label": item.label, "text": item.text, "url": item.url,
                     "map_id": item.map_id} for item in entries],
        "html": draft.html if draft else None,
        "map_url": draft.map_url if draft else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--as-of", type=datetime.fromisoformat, required=True)
    parser.add_argument("--telegram-app", default="https://t.me/relocant_media_belgrade_bot")
    parser.add_argument("--state-file", type=Path, help="Verified pointer with digest receipts")
    args = parser.parse_args()
    state = json.loads(args.state_file.read_bytes()) if args.state_file else None
    if args.state_file and not isinstance(state, dict):
        parser.error("--state-file must contain a JSON object")
    result = asyncio.run(preview(args.database, args.as_of, args.telegram_app, state=state))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
