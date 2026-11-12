import hashlib
from copy import deepcopy

import pytest
from pydantic import ValidationError

from relocant_media.ai.package import canonical_json
from relocant_media.domain.map_links import map_event_id
from relocant_media.ops.map_republication import (
    Request,
    deliver,
    latest_message_id,
    resolve_previous,
)
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


def test_previous_publication_reuses_only_a_draft_bound_to_the_sent_receipt():
    source = "https://www.beograd.rs/lat/servisne-informacije/elektricna-energija/a116619/notice.html"
    event = map_event_id(source)
    html_digest = hashlib.sha256(MESSAGE.html.encode()).hexdigest()
    draft = {"title": "Отключение света", "body": "Хутор Белаћевићи.", "claim_ids": ["place"]}
    package = {"outcome": {"draft": draft}, "presentation": {"native_map": {
        "point": {"source_url": source}, "message_html": MESSAGE.html,
        "message_html_sha256": html_digest,
    }}}
    context = hashlib.sha256(canonical_json(package).encode()).hexdigest()
    scope = {"version": 1, "package": package, "context_sha256": context,
             "request": {"message_id": 97, "event_id": event}}
    receipt = {"state": "sent", "original_id": 97, "message_ids": [98], "event_id": event,
               "context_sha256": context, "message_html_sha256": html_digest}
    state = {"map_republications": {"97": receipt}}
    request = Request(message_id=98, event_id=event, previous_review_sha256="0" * 64)
    original, selected = resolve_previous(request, state, scope)
    assert original == 97 and selected.model_dump(mode="json") == draft

    for change in ({"message_ids": [99]}, {"state": "sending"}, {"context_sha256": "0" * 64},
                   {"message_html_sha256": "0" * 64}, {"event_id": REQUEST.event_id}):
        with pytest.raises(ValueError, match="sent republication"):
            resolve_previous(request, {"map_republications": {"97": receipt | change}}, scope)


@pytest.mark.parametrize("uncertain", [False, True])
@pytest.mark.asyncio
async def test_republication_persists_intent_before_send_and_cannot_repeat(uncertain):
    writes = []
    messages = []
    state = {"status": "ready", "snapshot": {"id": "existing"}}

    def commit(current, sha, updates):
        payload = {**current, **updates}
        writes.append((deepcopy(payload), sha))
        assert payload["status"] == "ready" and payload["snapshot"] == state["snapshot"]
        return payload, "claim" if len(writes) == 1 else "final"

    async def send():
        assert writes[-1][0]["map_republications"]["97"]["state"] == "sending"
        messages.append(100)
        if uncertain:
            raise TimeoutError
        return 100

    if uncertain:
        with pytest.raises(RuntimeError, match="uncertain"):
            await deliver(REQUEST, state, "initial", commit, send, MESSAGE, "context")
        with pytest.raises(ValueError, match="reconciliation"):
            await deliver(REQUEST, writes[-1][0], "final", commit, send, MESSAGE, "context")
        assert writes[-1][0]["map_republications"]["97"]["state"] == "uncertain"
    else:
        sent = await deliver(REQUEST, state, "initial", commit, send, MESSAGE, "context")
        repeated = await deliver(REQUEST, writes[-1][0], "final", commit, send, MESSAGE, "context")
        assert sent == repeated and sent["message_ids"] == [100]
    assert messages == [100]
    assert [sha for _, sha in writes] == ["initial", "claim"]
