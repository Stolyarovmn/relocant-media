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


class Collector(Protocol):
    async def collect(self) -> list[RawItem]: ...
