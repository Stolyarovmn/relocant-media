import copy
import hashlib
import json
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import httpx
import pytest

from relocant_media.bot.api import PhotoUpload, TelegramBotAPI
from relocant_media.domain.map_links import map_event_id, map_event_url
from relocant_media.services.rich_delivery import (
    PreparedNativeMessage,
    prepare_native_transport,
    rich_receipt,
    send_native_message,
)
from relocant_media.services.transport_map_image import render_transport_map

LINE = [["44.8101", "20.4602"], ["44.8111", "20.4622"], ["44.8121", "20.4612"]]
HTML = '<p>Изменение маршрута</p><figure><img src="tg://photo?id=transport_map"/></figure>'
SOURCE_URL = "https://transport.example/notice"
TITLE, BODY = "Изменение маршрута", "Проверьте остановки перед поездкой."
EVENT_ID = "event_" + hashlib.sha256(SOURCE_URL.encode()).hexdigest()[:20]
MAP_URL = f"https://stolyarovmn.github.io/relocant-media/map/?event={EVENT_ID}"
MAP_MARKUP = {"inline_keyboard": [[{"text": "Открыть на карте", "url": MAP_URL}]]}


def multipart(request):
    content_type = request.headers["content-type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + request.content)
    parts = list(message.iter_parts())
    assert len(parts) == 4
    return {part.get_param("name", header="content-disposition"): part for part in parts}


def native_package():
    image = render_transport_map(copy.deepcopy(LINE))
    native = {"message_html": HTML, "point": {
        "mode": "transport_map_review", "source_url": SOURCE_URL,
        "transport_geometry": {"latitude": LINE[0][0], "longitude": LINE[0][1],
                               "line": copy.deepcopy(LINE)},
        "image_sha256": hashlib.sha256(image).hexdigest(),
    }}
    package = SimpleNamespace(
        presentation=SimpleNamespace(native_map=native),
        outcome=SimpleNamespace(draft=SimpleNamespace(title=TITLE, body=BODY)),
    )
    return package, image


def prepare(package, bot, *, media=None):
    return prepare_native_transport(
        package, SimpleNamespace(bot=bot), media,
        editorial_text=f"{TITLE}\n\n{BODY}\n\nИсточник: {SOURCE_URL}")


@pytest.mark.asyncio
async def test_rich_photo_sends_exact_png_and_rich_json_in_one_multipart_request(monkeypatch):
    monkeypatch.delenv("RELOCANT_MAP_TELEGRAM_APP", raising=False)
    requests = []
    package, png = native_package()

    def handler(request):
        requests.append(request)
        assert request.url.path.endswith("/sendRichMessage")
        parts = multipart(request)
        assert set(parts) == {"chat_id", "rich_message", "transport_map", "reply_markup"}
        assert parts["chat_id"].get_payload(decode=True) == b"-1001"
        assert json.loads(parts["rich_message"].get_payload(decode=True)) == {
            "html": HTML, "media": [{"id": "transport_map", "media": {
                "type": "photo", "media": "attach://transport_map"}}],
        }
        assert json.loads(parts["reply_markup"].get_payload(decode=True)) == MAP_MARKUP
        image = parts["transport_map"]
        assert image.get_filename() == "transport-map.png"
        assert image.get_content_type() == "image/png"
        assert image.get_payload(decode=True) == png
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 314}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot, prepared = prepare(package, TelegramBotAPI("fake-token", client))
        assert prepared == PreparedNativeMessage(
            HTML, PhotoUpload("transport-map.png", png), hashlib.sha256(png).hexdigest(),
            MAP_URL,
        )
        before = rich_receipt(prepared, state="sending")
        message_id = await send_native_message(bot, -1001, prepared)
        receipt = rich_receipt(prepared, state="sent", message_id=message_id)
    assert len(requests) == 1
    assert before["message_ids"] == [] and before["planned_message_count"] == 1
    button_digest = hashlib.sha256(
        json.dumps(MAP_MARKUP, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(),
    ).hexdigest()
    assert before["reply_markup_sha256"] == button_digest
    assert receipt == {
        "state": "sent", "message_ids": [314], "planned_message_count": 1,
        "image_sha256": hashlib.sha256(png).hexdigest(),
        "message_html_sha256": hashlib.sha256(HTML.encode()).hexdigest(),
        "reply_markup_sha256": button_digest, "map_url": MAP_URL,
    }


@pytest.mark.asyncio
async def test_existing_html_only_api_and_receipt_are_unchanged():
    requests = []
    html = '<p>Выставка</p><tg-map lat="44.8" long="20.4" zoom="14"/>'

    def handler(request):
        requests.append(request)
        assert request.headers["content-type"] == "application/json"
        assert json.loads(request.content) == {"chat_id": -1001, "rich_message": {"html": html}}
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await send_native_message(TelegramBotAPI("fake-token", client), -1001, html) == 8
    assert len(requests) == 1
    assert rich_receipt(html, state="sent", message_id=8) == {
        "state": "sent", "message_html_sha256": hashlib.sha256(html.encode()).hexdigest(),
        "planned_message_count": 1, "message_ids": [8],
    }


def test_map_event_id_reuses_source_url_canonicalization():
    assert map_event_id(SOURCE_URL + "/?utm_medium=telegram#map") == EVENT_ID
    assert map_event_id(SOURCE_URL + "?part=1") != EVENT_ID
    assert map_event_url(SOURCE_URL) == MAP_URL
    main_app = "https://t.me/relocant_media_belgrade_bot"
    assert map_event_url(SOURCE_URL, telegram_app=main_app) == f"{main_app}?startapp={EVENT_ID}"


@pytest.mark.parametrize("telegram_app", [
    "https://t.me/not_the_relocant_bot/map",
    "https://t.me/relocant_media_belgrade_bot/map?startapp=other",
    "https://t.me/relocant_media_belgrade_bot/other",
])
def test_invalid_telegram_app_config_uses_the_published_page(telegram_app):
    assert map_event_url(SOURCE_URL, telegram_app=telegram_app) == MAP_URL


@pytest.mark.asyncio
async def test_rich_utility_map_uses_registered_telegram_app_and_preserves_html(monkeypatch):
    app_url = "https://t.me/relocant_media_belgrade_bot/map"
    monkeypatch.setenv("RELOCANT_MAP_TELEGRAM_APP", app_url)
    package, _ = native_package()
    native = package.presentation.native_map
    native["point"] = {"mode": "utility_map_review", "source_url": SOURCE_URL}
    native["message_html"] = "<p>Карта отключения</p>"
    target_url = f"{app_url}?startapp={EVENT_ID}"

    def handler(request):
        assert request.url.path.endswith("/sendRichMessage")
        assert json.loads(request.content) == {
            "chat_id": -1001, "rich_message": {"html": native["message_html"]},
            "reply_markup": {"inline_keyboard": [[
                {"text": "Открыть на карте", "url": target_url},
            ]]},
        }
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 18}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot, prepared = prepare(package, TelegramBotAPI("fake-token", client))
        assert prepared.map_url == target_url
        assert await send_native_message(bot, -1001, prepared) == 18


@pytest.mark.asyncio
async def test_unapproved_map_button_does_not_call_telegram():
    def forbidden(request):
        pytest.fail("Кнопка с неподтверждённым URL не должна попадать в сеть")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError, match="map button URL is not approved"):
            await TelegramBotAPI("fake-token", client).send_rich_message(
                -1001, "<p>Карта</p>", map_url=MAP_URL + "&redirect=https://example.com",
            )


@pytest.mark.asyncio
async def test_unknown_png_delivery_does_not_retry_or_send_separate_media(monkeypatch):
    monkeypatch.delenv("RELOCANT_MAP_TELEGRAM_APP", raising=False)
    requests = []
    package, png = native_package()

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("Неизвестен исход запроса", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bot, prepared = prepare(package, TelegramBotAPI("fake-token", client))
        with pytest.raises(httpx.ReadTimeout):
            await send_native_message(bot, -1001, prepared)
    assert len(requests) == 1 and requests[0].url.path.endswith("/sendRichMessage")
    receipt = rich_receipt(prepared, state="uncertain")
    assert receipt["image_sha256"] == hashlib.sha256(png).hexdigest()
    assert receipt["map_url"] == MAP_URL
    assert receipt["message_ids"] == [] and receipt["planned_message_count"] == 1


@pytest.mark.parametrize("change", [
    "hash", "line", "no_line", "no_hash", "wrong_mode", "no_image_tag", "duplicate_tag",
])
def test_transport_attachment_is_bound_to_reviewed_geometry_and_html(change):
    package, _ = native_package()
    point = package.presentation.native_map["point"]
    if change == "hash":
        point["image_sha256"] = "0" * 64
    elif change == "line":
        point["transport_geometry"]["line"][1][0] = "44.8151"
    elif change == "no_line":
        point["transport_geometry"]["line"] = []
    elif change == "no_hash":
        point.pop("image_sha256")
    elif change == "wrong_mode":
        point["mode"] = "point_map_review"
    elif change == "no_image_tag":
        package.presentation.native_map["message_html"] = "<p>Без изображения</p>"
    else:
        package.presentation.native_map["message_html"] += HTML
    with pytest.raises(ValueError):
        prepare(package, SimpleNamespace(send_rich_message=lambda *args: None))


def test_transport_map_without_line_keeps_the_html_only_path():
    package, _ = native_package()
    point = package.presentation.native_map["point"]
    point["transport_geometry"]["line"] = []
    point.pop("image_sha256")
    package.presentation.native_map["message_html"] = "<p>Только карта точки</p>"
    _, prepared = prepare(package, SimpleNamespace(send_rich_message=lambda *args: None))
    assert prepared.photo is None and prepared.image_sha256 is None
    assert "image_sha256" not in rich_receipt(prepared, state="sending")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_photo", "unused_photo", "jpg", "invalid_png"])
async def test_invalid_rich_attachment_never_makes_a_request(case):
    def forbidden(request):
        pytest.fail("Невалидное вложение не должно попадать в сеть")

    png = render_transport_map(copy.deepcopy(LINE))
    photo = PhotoUpload("transport-map.png", png)
    html = HTML
    if case == "missing_photo":
        photo = None
    elif case == "unused_photo":
        html = "<p>Без карты</p>"
    elif case == "jpg":
        photo = PhotoUpload("transport-map.jpg", b"\xff\xd8\xffsynthetic")
    else:
        photo = PhotoUpload("transport-map.png", b"invalid")
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        with pytest.raises(ValueError):
            await TelegramBotAPI("fake-token", client).send_rich_message(-1001, html, photo=photo)
