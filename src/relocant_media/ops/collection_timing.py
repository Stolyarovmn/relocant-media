"""Длительность сборщиков без адресов, текстов и идентификаторов заявок."""

from collections.abc import Iterator
from contextlib import contextmanager
from time import monotonic

COLLECTION_GROUPS = (
    "government_source", "reader_links", "published_watch", "city_pages", "municipality_pages",
    "water_utility", "traffic_secretariat", "venues", "media", "guides", "channel_previews",
    "channel_evidence", "news_feeds", "news_evidence", "utility_maps", "transport_map",
    "queue_triage", "reader_feedback",
)


@contextmanager
def measure_collection(seconds: dict[str, float], group: str) -> Iterator[None]:
    start = monotonic()
    try:
        yield
    finally:
        seconds[group] = seconds.get(group, 0.0) + (monotonic() - start)
