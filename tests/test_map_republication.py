from copy import deepcopy

import pytest
from pydantic import ValidationError

from relocant_media.ops.map_republication import Request, deliver, latest_message_id
from relocant_media.services.rich_delivery import PreparedNativeMessage

REQUEST = Request(message_id=97, event_id="event_4c8ef6e8ee1b07e6f721")
MESSAGE = PreparedNativeMessage(
    "<p>Отключение света</p>",
    map_url="https://t.me/relocant_media_belgrade_bot?startapp=event_4c8ef6e8ee1b07e6f721",
)


def test_public_target_requires_real_channel_posts():
    assert latest_message_id('''
      <div class="tgme_widget_message" data-post="eduvbelgrad/97"></div>
      <div class="tgme_widget_message" data-post="eduvbelgrad/96"></div>
      <div class="tgme_widget_message" data-post="other/999"></div>
      <div data-post="eduvbelgrad/333"></div>''') == 97
    with pytest.raises(ValueError, match="no messages"):
        latest_message_id("<html>Пустая лента</html>")
    with pytest.raises(ValidationError):
        Request.model_validate({"message_id": "97", "event_id": REQUEST.event_id})


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.asyncio
async def test_republication_persists_intent_before_send_and_cannot_repeat(uncertain):
    writes = []
    messages = []
    state = {"status": "ready", "snapshot": {"id": "existing"}}

    def write(payload, sha):
        writes.append((deepcopy(payload), sha))
        assert payload["status"] == "ready" and payload["snapshot"] == state["snapshot"]
        return "claim" if len(writes) == 1 else "final"

    async def send():
        assert writes[-1][0]["map_republications"]["97"]["state"] == "sending"
        messages.append(100)
        if uncertain:
            raise TimeoutError
        return 100

    if uncertain:
        with pytest.raises(RuntimeError, match="uncertain"):
            await deliver(REQUEST, state, "initial", write, send, MESSAGE, "context")
        with pytest.raises(ValueError, match="reconciliation"):
            await deliver(REQUEST, writes[-1][0], "final", write, send, MESSAGE, "context")
        assert writes[-1][0]["map_republications"]["97"]["state"] == "uncertain"
    else:
        sent = await deliver(REQUEST, state, "initial", write, send, MESSAGE, "context")
        repeated = await deliver(REQUEST, writes[-1][0], "final", write, send, MESSAGE, "context")
        assert sent == repeated and sent["message_ids"] == [100]
    assert messages == [100]
    assert [sha for _, sha in writes] == ["initial", "claim"]
