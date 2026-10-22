"""Scoped change evidence for official Welcome to Serbia temporary-residence guidance."""

from __future__ import annotations

import hashlib
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from relocant_media.collectors.base import RawItem
from relocant_media.collectors.page_snapshot import normalize_visible_text
from relocant_media.sources.registry import SourceConfig

MAX_BYTES = 4_000_000
GUIDANCE_TITLE = (
    "Potrebna vam je dozvola za privremeni boravak, odnosno dozvola za privremni "
    "boravak i rad (jedinstvena dozvola)"
)
_REQUIRED_SECTIONS = (
    "Šta je privremeni boravak?",
    "Privremeni boravak i rad (jedinstvena dozvola)",
    "Dozvola za privremeni boravak",
    "Kako do dozvole za privremeni boravak i jedinstvenu dozvolu",
    "Zahtev za odobrenje privremenog boravka i jedinstvene dozvole",
    "Kako produžiti dozvolu za privremeni boravak, odnosno jedinstvenu dozvolu",
    "Odbijanje zahteva za privremeni boravak, odnosno jedinstvenu dozvolu",
    "U slučaju nestanka dozvole za privremeni boravak i jedinstvene dozvole",
)
_END_MARKER = "Podnesite zahtev"


def _allowed_url(value: str, source: SourceConfig) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise ValueError("unapproved Welcome Serbia guidance URL") from error

    source_url = urlsplit(str(source.url))
    allowed_hosts = {source_url.hostname, *source.extraction.approved_hosts}
    if (
        parsed.scheme.lower() != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.path.rstrip("/") != source_url.path.rstrip("/")
        or parsed.fragment
    ):
        raise ValueError("unapproved Welcome Serbia guidance URL")
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def _scoped_guidance_text(html: str) -> str:
    if len(html.encode("utf-8")) > MAX_BYTES:
        raise ValueError("Welcome Serbia guidance response too large")

    lines = normalize_visible_text(html).splitlines()
    try:
        start = lines.index(GUIDANCE_TITLE)
    except ValueError as error:
        raise ValueError("Welcome Serbia guidance title missing") from error

    try:
        end = lines.index(_END_MARKER, start + 1)
    except ValueError as error:
        raise ValueError("Welcome Serbia guidance end marker missing") from error

    scoped = lines[start:end]
    if not scoped or scoped[0] != GUIDANCE_TITLE:
        raise ValueError("Welcome Serbia guidance block missing")

    section_positions: list[int] = []
    for section in _REQUIRED_SECTIONS:
        try:
            section_positions.append(scoped.index(section))
        except ValueError as error:
            raise ValueError(f"Welcome Serbia required section missing: {section}") from error
    if section_positions != sorted(section_positions) or len(set(section_positions)) != len(
        section_positions
    ):
        raise ValueError("Welcome Serbia guidance section order invalid")

    body = "\n".join(scoped).strip()
    if len(body) < 2_500:
        raise ValueError("Welcome Serbia guidance body shorter than 2500 characters")
    return body


def parse_welcome_serbia_guidance(html: str, source: SourceConfig) -> RawItem:
    """Hash only the authoritative immigration-guidance block, excluding page chrome."""
    if source.extraction.strategy != "welcome_serbia_guidance":
        raise ValueError(source.extraction.strategy)
    if not source.policy.high_risk or source.policy.auto_publish:
        raise ValueError("Welcome Serbia guidance must stay high-risk and review-only")

    body = _scoped_guidance_text(html)
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    canonical_url = str(source.url)
    return RawItem(
        source_id=source.id,
        external_id=f"welcome-serbia-guidance-{content_hash}",
        url=canonical_url,
        title=GUIDANCE_TITLE,
        body=body,
        published_at=None,
        canonical_url=canonical_url,
        source_html=html,
    )


class WelcomeSerbiaGuidanceCollector:
    def __init__(self, source: SourceConfig, client: httpx.AsyncClient) -> None:
        if source.extraction.strategy != "welcome_serbia_guidance":
            raise ValueError(source.extraction.strategy)
        if not source.policy.high_risk or source.policy.auto_publish:
            raise ValueError("Welcome Serbia guidance must stay high-risk and review-only")
        self._source = source
        self._client = client
        _allowed_url(str(source.url), source)

    async def _fetch(self) -> str:
        current = _allowed_url(str(self._source.url), self._source)
        for _ in range(6):
            async with self._client.stream(
                "GET",
                current,
                follow_redirects=False,
                timeout=25,
                headers={"User-Agent": "RelocantMedia source monitoring/1.0"},
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Welcome Serbia redirect without location")
                    current = _allowed_url(urljoin(current, location), self._source)
                    continue
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared = int(content_length)
                    except ValueError:
                        declared = 0
                    if declared > MAX_BYTES:
                        raise ValueError("Welcome Serbia guidance response too large")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_BYTES:
                        raise ValueError("Welcome Serbia guidance response too large")
                return data.decode(response.encoding or "utf-8")
        raise ValueError("Welcome Serbia redirect limit")

    async def collect(self) -> list[RawItem]:
        return [parse_welcome_serbia_guidance(await self._fetch(), self._source)]
