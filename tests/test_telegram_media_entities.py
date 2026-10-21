import json
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from relocant_media.bot.api import PhotoUpload, TelegramBotAPI


def _parts(request: httpx.Request) -> dict[str, bytes]:
    raw = (
        f"Content-Type: {request.headers['content-type']}\r\nMIME-Version: 1.0\r\n\r\n"
    ).encode() + request.content
    message = BytesParser(policy=policy.default).parsebytes(raw)
    return {
        item.get_param("name", header="content-disposition"): item.get_payload(decode=True)
        for item in message.iter_parts()
    }


@pytest.mark.asyncio
async def test_photo_and_album_send_caption_text_link_entities() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("sendPhoto"):
            result: object = {"message_id": 1}
        else:
            result = [{"message_id": 2}, {"message_id": 3}]
        return httpx.Response(200, json={"ok": True, "result": result}, request=request)

    photo = PhotoUpload("x.png", b"\x89PNG\r\n\x1a\npayload")
    entity = {
        "type": "text_link",
        "offset": 8,
        "length": 8,
        "url": "https://example.test/source",
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot = TelegramBotAPI("fake", client)
        assert await bot.send_photo(
            "-1001", photo, "Новость\nИсточник", (entity,)
        ) == 1
        assert await bot.send_media_group(
            "-1001", (photo, photo), "Новость\nИсточник", (entity,)
        ) == [2, 3]

    single = _parts(requests[0])
    album = _parts(requests[1])
    assert json.loads(single["caption_entities"]) == [entity]
    media = json.loads(album["media"])
    assert media[0]["caption_entities"] == [entity]
    assert "caption_entities" not in media[1]
