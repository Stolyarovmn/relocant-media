"""Observed local mentions, not attendance estimates or publication approval."""
import re
from collections.abc import Iterable, Mapping
from typing import Any


def normalize(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.casefold()))


def venue_attention(leads: Iterable[Any], aliases: Mapping[str, tuple[str, ...]]) -> dict[str, Any]:
    mentions: dict[str, dict[str, str]] = {key: {} for key in aliases}
    channels: dict[str, set[str]] = {key: set() for key in aliases}
    for lead in leads:
        text = " " + normalize(lead.text) + " "
        for source_id, names in aliases.items():
            if any(" " + normalize(name) + " " in text for name in names if name.strip()):
                mentions[source_id][lead.url] = lead.channel
                channels[source_id].add(lead.channel)
    return {key: {"status": "observed_mentions" if mentions[key] else "unknown",
                  "distinct_channels": len(channels[key]),
                  "post_count": len(mentions[key]), "urls": sorted(mentions[key]),
                  "attendance": None, "publication_permission": False}
            for key in aliases}


def source_priorities(attention: dict[str, Any]) -> dict[str, int]:
    # These are editorial queue bands, never probabilities or popularity scores.
    return {key: min(2, int(value.get("distinct_channels", 0)))
            for key, value in attention.items()}
