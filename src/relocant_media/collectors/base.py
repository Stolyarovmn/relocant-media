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


class Collector(Protocol):
    async def collect(self) -> list[RawItem]: ...
