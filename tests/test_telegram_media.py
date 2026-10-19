import json
from email import policy
from email.parser import BytesParser
from pathlib import Path

import httpx
import pytest

from relocant_media.bot.api import PhotoUpload, TelegramAPIError, TelegramBotAPI
from relocant_media.bot.media_publisher import StagingMediaPublisher
from relocant_media.bot.publisher import ProductionTargetBlockedError


def photo(tmp_path: Path, name: str = "photo.png") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\nlocal-image-payload")
    return path


def parts(request: httpx.Request) -> dict[str, bytes]:
    raw = (
        f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n"
    ).encode() + request.content
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return {
        item.get_param("name", header="content-disposition"): item.get_payload(decode=True)
        for item in message.iter_parts()
    }


@pytest.mark.asyncio
async def test_photo_and_album_are_local_multipart(tmp_path: Path) -> None:
    paths = [photo(tmp_path, f"{index}.png") for index in range(2)]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        result: object = {"message_id": 21}
        if request.url.path.endswith("sendMediaGroup"):
            result = [{"message_id": 22}, {"message_id": 23}]
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = StagingMediaPublisher(TelegramBotAPI("fake", client), "-1001", "-1002")
        assert await publisher.publish("Подпись", paths[:1]) == [21]
        assert await publisher.publish("Альбом", paths) == [22, 23]
    single, album = map(parts, requests)
    assert single["photo"] == paths[0].read_bytes()
    assert single["caption"].decode() == "Подпись"
    assert album["photo0"] == paths[0].read_bytes()
    assert album["photo1"] == paths[1].read_bytes()
    assert album["chat_id"] == b"-1001"
    assert json.loads(album["media"]) == [
        {"type": "photo", "media": "attach://photo0", "caption": "Альбом"},
        {"type": "photo", "media": "attach://photo1"},
    ]
    assert b"image/png" in requests[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "result", [[], [{"message_id": 1}], [{"message_id": 1}, {"message_id": True}],
               [{"message_id": 1}, {"message_id": 1}]]
)
async def test_album_requires_all_unique_valid_ids(tmp_path: Path, result: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("fake", client)
        with pytest.raises(TelegramAPIError):
            await bot.send_media_group("-1001", [photo(tmp_path), photo(tmp_path, "other.png")])


@pytest.mark.asyncio
async def test_invalid_input_never_reaches_transport(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid input must not send HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("fake", client)
        with pytest.raises(ProductionTargetBlockedError):
            StagingMediaPublisher(bot, " -1001 ", "-1001")
        publisher = StagingMediaPublisher(bot, "-1001", "-1002")
        valid = photo(tmp_path)
        for paths in ([], [valid] * 11):
            with pytest.raises(ValueError):
                await publisher.publish("Подпись", paths)
        with pytest.raises(ValueError):
            await publisher.publish("😀" * 513, [valid])
        with pytest.raises(ValueError):
            await bot.send_media_group("-1001", [valid])
        invalid = tmp_path / "invalid.png"
        invalid.write_bytes(b"not a photo")
        with pytest.raises(ValueError):
            await bot.send_photo("-1001", invalid)


@pytest.mark.asyncio
async def test_jpeg_upload_and_api_failure(tmp_path: Path) -> None:
    jpeg = tmp_path / "photo.jpg"
    jpeg.write_bytes(b"\xff\xd8\xffjpeg-payload")

    def handler(request: httpx.Request) -> httpx.Response:
        assert b"image/jpeg" in request.content
        return httpx.Response(200, json={"ok": False, "description": "rejected"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(TelegramAPIError, match="rejected"):
            await TelegramBotAPI("fake", client).send_photo("-1001", jpeg)


@pytest.mark.asyncio
async def test_immutable_upload_does_not_reread_changed_file(tmp_path: Path) -> None:
    path = photo(tmp_path)
    expected = path.read_bytes()
    upload = PhotoUpload(path.name, expected)
    path.write_bytes(b"changed after verification")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("sendPhoto"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
        return httpx.Response(
            200, json={"ok": True, "result": [{"message_id": 2}, {"message_id": 3}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = StagingMediaPublisher(TelegramBotAPI("fake", client), "-1001", "-1002")
        assert await publisher.publish("Проверено", [upload]) == [1]
        path.unlink()
        assert await publisher.publish("Проверено", [upload, upload]) == [2, 3]
    assert parts(requests[0])["photo"] == expected
    assert parts(requests[1])["photo0"] == expected
    assert parts(requests[1])["photo1"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["../photo.png", "dir/photo.png", "bad\r\n.png", "bad.jpg"])
async def test_immutable_upload_checks_name_and_magic(name: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid upload must not send HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            await TelegramBotAPI("fake", client).send_photo(
                "-1001", PhotoUpload(name, b"\x89PNG\r\n\x1a\npayload")
            )
