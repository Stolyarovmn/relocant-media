import httpx

from relocant_media.collectors.base import Collector
from relocant_media.collectors.beograd_news import BelgradeNewsListCollector
from relocant_media.collectors.page_snapshot import PageSnapshotCollector
from relocant_media.sources.registry import SourceConfig


class UnsupportedCollectorStrategyError(ValueError):
    pass


def build_collector(source: SourceConfig, client: httpx.AsyncClient) -> Collector:
    strategy = source.extraction.strategy
    if strategy == "beograd_news_list":
        return BelgradeNewsListCollector(source, client)
    if strategy == "page_change_detection":
        return PageSnapshotCollector(source, client)
    raise UnsupportedCollectorStrategyError(strategy)
