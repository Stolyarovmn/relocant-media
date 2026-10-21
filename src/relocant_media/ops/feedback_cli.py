"""Normalize an authorized UI observation without connecting to a database."""

import argparse
import json
from pathlib import Path

from relocant_media.analytics.feedback import (
    feedback_summary,
    normalize_observation,
    save_observation,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observation", required=True, type=Path)
    parser.add_argument("--metadata", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = normalize_observation(
        json.loads(args.observation.read_text(encoding="utf-8")),
        json.loads(args.metadata.read_text(encoding="utf-8")),
    )
    path = save_observation(args.output, payload)
    print(json.dumps(dict(file=str(path), summary=feedback_summary([payload])), ensure_ascii=False))


if __name__ == "__main__":
    main()
