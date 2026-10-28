import httpx

from relocant_media.collectors.base import Collector
from relocant_media.collectors.beoelektrane_network_works import BeoelektraneNetworkWorksCollector
from relocant_media.collectors.beograd_news import BelgradeNewsListCollector
from relocant_media.collectors.bgprevoz_changes import BgprevozChangesCollector
from relocant_media.collectors.bgprevoz_news import BgprevozNewsCollector
from relocant_media.collectors.bitefartcafe_events import BitefArtCafeEventsCollector
from relocant_media.collectors.bvk_planned_works import BvkPlannedWorksCollector
from relocant_media.collectors.cukarica_news import CukaricaNewsCollector
from relocant_media.collectors.eds_belgrade_outages import EdsBelgradeOutagesCollector
from relocant_media.collectors.euprava_service import EupravaServiceCollector
from relocant_media.collectors.health_duty import HealthDutyCollector
from relocant_media.collectors.musicology_blog import MusicologyBlogCollector
from relocant_media.collectors.natural_history_news import NaturalHistoryNewsCollector
from relocant_media.collectors.official_guide import OfficialGuideCollector
from relocant_media.collectors.page_snapshot import PageSnapshotCollector
from relocant_media.collectors.srbija_gov_article import SrbijaGovArticleCollector
from relocant_media.collectors.srbijavoz_news import SrbijavozNewsCollector
from relocant_media.collectors.tob_events import TobEventsCollector
from relocant_media.collectors.venue_feed import VenueFeedCollector
from relocant_media.collectors.welcome_serbia_guidance import WelcomeSerbiaGuidanceCollector
from relocant_media.collectors.yugoslavia_programs import YugoslaviaProgramsCollector
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
    if strategy == "beoelektrane_network_works":
        return BeoelektraneNetworkWorksCollector(source, client)
    if strategy == "bgprevoz_changes":
        return BgprevozChangesCollector(source, client)
    if strategy == "bgprevoz_news":
        return BgprevozNewsCollector(source, client)
    if strategy == "health_duty":
        return HealthDutyCollector(source, client)
    if strategy == "euprava_service":
        return EupravaServiceCollector(source, client)
    if strategy == "tob_events":
        return TobEventsCollector(source, client)
    if strategy == "official_guide":
        return OfficialGuideCollector(source, client)
    if strategy == "welcome_serbia_guidance":
        return WelcomeSerbiaGuidanceCollector(source, client)
    if strategy == "yugoslavia_programs":
        return YugoslaviaProgramsCollector(source, client)
    if strategy == "natural_history_news":
        return NaturalHistoryNewsCollector(source, client)
    if strategy == "bitefartcafe_events":
        return BitefArtCafeEventsCollector(source, client)
    if strategy == "musicology_blog":
        return MusicologyBlogCollector(source, client)
    if strategy == "srbija_gov_article":
        return SrbijaGovArticleCollector(source, client)
    raise UnsupportedCollectorStrategyError(strategy)
