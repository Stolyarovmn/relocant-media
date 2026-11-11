"""Воспроизводимый локальный план рубрик без API, аккаунтов и отправки публикаций."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from relocant_media.domain.city_profile import load_city_profile
from relocant_media.services.editorial_products import rubric_plan


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True)
    parser.add_argument("--cities-root", type=Path)
    parser.add_argument("--history", type=Path, help="JSON-массив опубликованных карточек")
    parser.add_argument("--as-of", required=True, help="ISO8601 с часовым поясом")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    profile = load_city_profile(args.city, cities_root=args.cities_root)
    history = json.loads(args.history.read_text(encoding="utf-8")) if args.history else []
    result = rubric_plan(profile, history, now=datetime.fromisoformat(args.as_of))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
