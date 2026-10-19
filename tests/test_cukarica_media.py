from pathlib import Path

import httpx
import pytest

from relocant_media.collectors.cukarica_media import (
    MAX_IMAGE_BYTES,
    CukaricaMediaCandidate,
    download_cukarica_media,
    parse_cukarica_media,
)
from relocant_media.collectors.cukarica_news import CukaricaExtractionError

ARTICLE = "https://www.cukarica.rs/vest/655-festival/"
IMAGE = "https://www.cukarica.rs/admin/uploads/images/festival_123.jpg"
PNG = b"\x89PNG\r\n\x1a\n" + b"test-image"
HTML = f'''<meta property="og:image" content="https://evil.test/logo.png">
<div class="page-full-width"><h1>Фестиваль</h1><!-- Featured Image -->
<div><img src="{IMAGE}" alt="Афиша"></div><!-- Excerpt --><div>Текст</div>
<div class="page-content">Текст</div><!-- Related News -->
<img src="https://evil.test/related.png"></div><footer><img src="/logo.png"></footer>'''


def candidate():
    return CukaricaMediaCandidate(IMAGE, "Афиша", ARTICLE)


def test_only_featured_image_is_discovered():
    assert parse_cukarica_media(HTML, ARTICLE) == [candidate()]


def test_og_image_alone_does_not_establish_article_image():
    assert parse_cukarica_media(HTML.replace(f'<img src="{IMAGE}" alt="Афиша">', ""), ARTICLE) == []


@pytest.mark.parametrize(
    "url", ["http://www.cukarica.rs/vest/655-festival/", "https://evil.test/vest/655-festival/"]
)
def test_invalid_article_is_rejected(url):
    with pytest.raises(CukaricaExtractionError):
        parse_cukarica_media(HTML, url)


def test_changed_markup_is_rejected():
    with pytest.raises(CukaricaExtractionError):
        parse_cukarica_media(HTML.replace("<!-- Excerpt -->", "<!-- Other -->"), ARTICLE)


def test_download_signature_hash_and_immutable_directory(tmp_path):
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PNG))
    ) as c:
        result = download_cukarica_media(candidate(), tmp_path / "snapshot", c)
        assert result.path.read_bytes() == PNG
        assert result.content_type == "image/png"
        assert len(result.sha256) == 64
        with pytest.raises(FileExistsError):
            download_cukarica_media(candidate(), tmp_path / "snapshot", c)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.test/x.jpg",
        "http://www.cukarica.rs/a.jpg",
        "https://www.cukarica.rs/admin/uploads/images/../x.jpg",
    ],
)
def test_redirect_is_validated_before_next_request(tmp_path, location):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": location})

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(CukaricaExtractionError):
            download_cukarica_media(candidate(), tmp_path / "snapshot", c)
    assert len(requests) == 1
    assert not (tmp_path / "snapshot").exists()


@pytest.mark.parametrize(
    "payload,headers",
    [(b"<html>not an image</html>", {}), (PNG, {"content-length": str(MAX_IMAGE_BYTES + 1)})],
)
def test_bad_signature_or_size_is_rejected(tmp_path, payload, headers):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, content=payload, headers=headers)
        )
    ) as c:
        with pytest.raises(CukaricaExtractionError):
            download_cukarica_media(candidate(), tmp_path / "snapshot", c)
    assert not (tmp_path / "snapshot").exists()


def test_automatic_redirect_client_is_rejected(tmp_path):
    with httpx.Client(
        follow_redirects=True,
        transport=httpx.MockTransport(lambda r: pytest.fail("network should not be called")),
    ) as c:
        with pytest.raises(CukaricaExtractionError):
            download_cukarica_media(candidate(), Path(tmp_path) / "snapshot", c)


def test_stream_without_length_is_bounded(tmp_path):
    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield PNG
            yield b"x" * MAX_IMAGE_BYTES

    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=OversizedStream()))
    ) as c:
        with pytest.raises(CukaricaExtractionError, match="size limit"):
            download_cukarica_media(candidate(), tmp_path / "snapshot", c)
    assert not (tmp_path / "snapshot").exists()


def test_allowed_redirect_and_jpeg(tmp_path):
    visited = []

    def handler(request):
        visited.append(str(request.url))
        if len(visited) == 1:
            return httpx.Response(302, headers={"location": "/admin/uploads/images/new.jpg"})
        return httpx.Response(200, content=b"\xff\xd8\xfftest")

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        result = download_cukarica_media(candidate(), tmp_path / "snapshot", c)
    assert result.content_type == "image/jpeg"
    assert result.source_url == "https://www.cukarica.rs/admin/uploads/images/new.jpg"
    assert len(visited) == 2
