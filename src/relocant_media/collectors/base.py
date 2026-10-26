from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RawItem:
    source_id: str
    external_id: str
    url: str
    title: str
    body: str
    published_at: datetime | None
    # Optional internal identity for list pages where many independent notices share
    # one public citation URL. The public `url` remains untouched for evidence/output.
    canonical_url: str | None = None
    # Optional HTML from the exact source-page response already used by the collector.
    # It is inert provenance input only: downstream media discovery must not re-fetch it.
    source_html: str | None = None
    # Extra image hosts explicitly bound by a source-specific collector. The source-page
    # host itself is always allowed by the generic media candidate extractor.
    approved_image_hosts: tuple[str, ...] = ()
    # A day timestamp is a scheduling boundary, never an observed publication clock.
    published_at_precision: str = "unknown"


class Collector(Protocol):
    async def collect(self) -> list[RawItem]: ...
