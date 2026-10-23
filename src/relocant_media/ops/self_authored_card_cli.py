"""Generate a first-party fallback card bound to an existing reviewed package."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from relocant_media.services.self_authored_card import create_self_authored_card_bundle


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create review input for a deterministic first-party Telegram card",
    )
    parser.add_argument("--raw-item-id", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    if os.environ.get("APP_ENV", "").lower() != "production":
        parser.exit(1, "Self-authored card blocked: APP_ENV=production required\n")
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        parser.exit(1, "Self-authored card blocked: explicit DATABASE_URL required\n")

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            result = create_self_authored_card_bundle(
                session,
                args.raw_item_id,
                args.output,
            )
    except Exception as error:
        parser.exit(1, f"Self-authored card blocked: {error}\n")
    finally:
        engine.dispose()

    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
