import httpx

from relocant_media.collectors.base import Collector
from relocant_media.collectors.beograd_news import BelgradeNewsListCollector
from relocant_media.collectors.bvk_planned_works import BvkPlannedWorksCollector
from relocant_media.collectors.cukarica_news import CukaricaNewsCollector
from relocant_media.collectors.eds_belgrade_outages import EdsBelgradeOutagesCollector
from relocant_media.collectors.page_snapshot import PageSnapshotCollector
from relocant_media.collectors.srbijavoz_news import SrbijavozNewsCollector
from relocant_media.collectors.venue_feed import VenueFeedCollector
from relocant_media.sources.registry import SourceConfig


class UnsupportedCollectorStrategyError(ValueError):
    pass


def build_collector(source: SourceConfig, client: httpx.AsyncClient) -> Collector:
    strategy = source.extraction.strategy
    if strategy == "beograd_news_list":
        return BelgradeNewsListCollector(source, client)
    if strategy == "cukarica_news":
        return CukaricaNewsCollector(source, client)
    if strategy == "venue_feed":
        return VenueFeedCollector(source, client)
    if strategy == "page_change_detection":
        return PageSnapshotCollector(source, client)
    if strategy == "bvk_planned_works":
        return BvkPlannedWorksCollector(source, client)
    if strategy == "eds_belgrade_outages":
        return EdsBelgradeOutagesCollector(source, client)
    if strategy == "srbijavoz_news":
        return SrbijavozNewsCollector(source, client)
    raise UnsupportedCollectorStrategyError(strategy)
