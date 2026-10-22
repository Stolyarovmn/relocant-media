from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.yugoslavia_programs import YugoslaviaProgramsCollector
from relocant_media.sources.registry import load_source_file

SOURCE_PATH = Path("cities/belgrade/sources/venue_yugoslavia.yaml")


@pytest.mark.asyncio
async def test_cloudflare_525_is_transport_failure() -> None:
    source = load_source_file(SOURCE_PATH)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(525, request=request)

    async with httpx.AsyncClient(
        trust_env=False,
        transport=httpx.MockTransport(handler),
    ) as client:
        collector = YugoslaviaProgramsCollector(source, client)
        with pytest.raises(httpx.ConnectError, match="upstream TLS handshake failed"):
            await collector.collect()
