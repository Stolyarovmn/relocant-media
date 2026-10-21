"""Read-only event-level newsroom inbox built from the canonical editorial database."""

import argparse
import json

from relocant_media.config import settings
from relocant_media.services.newsroom_control import build_newsroom_inbox
from relocant_media.storage.db import SessionFactory


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", default=settings.city_id)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    with SessionFactory() as session:
        payload = build_newsroom_inbox(session, city=args.city, limit=args.limit)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
