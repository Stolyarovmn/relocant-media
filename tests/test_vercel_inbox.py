import copy
import json
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_actions_runtime import context as _context
from test_actions_runtime import execute
from test_actions_runtime import state as _state
from test_owner_review_intake import OWNER, OWNER_CHAT, OwnerBot, _review
from test_private_intake import engine as _engine

from relocant_media.ops import actions_runtime as runtime
from relocant_media.ops import intake_batch, vercel_inbox
from relocant_media.ops.serverless_snapshot import export_intake_state, open_draft_routes
from relocant_media.ops.serverless_snapshot import main as export_main
from relocant_media.services.serverless_intake import import_inbox
from relocant_media.storage.models import Submission, TelegramUpdateReceipt

engine, state, context = _engine, _state, _context
ORIGIN = "https://reader-ingress.vercel.app"
CONFIG = {"version": 1, "backend": "vercel", "phase": "active", "url": ORIGIN,
          "seed_sha256": "a" * 64}


def envelope(sequence=1, update_id=501):
    return {
        "sequence": sequence,
        "update": {"update_id": update_id, "message": {
            "message_id": 101, "from": {"id": 42}, "chat": {"id": 42, "type": "private"},
        }},
        "draft": {"reference": "RM-1234-5678", "kind": "technical", "status": "collecting",
                  "telegram_user_id": 42, "telegram_message_id": 101, "payload": {
                      "reader_intake": True, "public_reference": "RM-1234-5678", "chat_id": 42,
                      "city": "belgrade", "text": "Ошибка карты", "attachments": [
                          {"kind": "photo", "file_id": "private-file", "file_unique_id": "stable"},
                      ],
                  }},
        "notice": {"key": "prompt:101", "status": "sent", "telegram_message_id": 201},
    }


def test_private_http_protocol_has_no_cli_dependency():
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == ORIGIN + "/api/transfer"
        assert request.headers["Authorization"] == "Bearer private-token"
        value = json.loads(request.content)
        if value["operation"] == "peek":
            assert value == {"operation": "peek", "after": 4, "limit": 30}
            return httpx.Response(200, json={"status": "ready", "events": [envelope(5)]})
        assert value == {"operation": "ack", "through": 5, "snapshot_sha256": "b" * 64}
        return httpx.Response(200, json={"status": "ready"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert vercel_inbox.peek_inbox(ORIGIN, "private-token", 4, client=client) == [envelope(5)]
        vercel_inbox.acknowledge_inbox(ORIGIN, "private-token", 5, "b" * 64, client=client)
    assert len(requests) == 2


@pytest.mark.parametrize("url", [
    "http://reader-ingress.vercel.app", "https://127.0.0.1", "https://vercel.app",
    "https://reader-ingress.vercel.app.attacker.org", "https://u:p@reader-ingress.vercel.app",
    ORIGIN + ":443", ORIGIN + "/api/transfer", ORIGIN + "?url=https://evil.invalid",
])
def test_transport_rejects_arbitrary_sources_before_network(url):
    with pytest.raises(ValueError):
        vercel_inbox.peek_inbox(url, "token", 0)


@pytest.mark.parametrize("result", [
    {"status": "ready", "events": [envelope(2)]},
    {"status": "ready", "events": [envelope(1), envelope(3)]},
    {"status": "ready", "events": [envelope(1), envelope(1)]},
    {"status": "ready", "events": [envelope(1)] * 31},
    {"status": "blocked", "events": []},
])
def test_transport_rejects_noncontiguous_or_invalid_batches(result):
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=result))
    with httpx.Client(transport=transport) as c:
        with pytest.raises(ValueError):
            vercel_inbox.peek_inbox(ORIGIN, "token", 0, client=c)


def test_response_bound_and_redirects():
    for response in [httpx.Response(200, content=b" " * (512 * 1024 + 1)),
                     httpx.Response(307, headers={"Location": "https://evil.invalid"})]:
        transport = httpx.MockTransport(lambda _, response=response: response)
        with httpx.Client(transport=transport) as c:
            with pytest.raises((ValueError, httpx.HTTPStatusError)):
                vercel_inbox.peek_inbox(ORIGIN, "token", 0, client=c)


@pytest.mark.asyncio
async def test_draft_reference_media_receipts_and_owner_callback_survive_replay(engine):
    bot = OwnerBot([], callback_expired=True)
    event = envelope()
    with Session(engine) as session:
        owner_id = _review(session).id
    callback = {"sequence": 2, "update": {"update_id": 502, "callback_query": {
        "id": "cb", "from": {"id": OWNER}, "data": f"or:publish:{owner_id}",
        "message": {"message_id": 501, "chat": {"id": OWNER_CHAT}},
    }}}
    for _ in range(2):
        assert await import_inbox(engine, bot, [event, callback], owner_user_id=OWNER,
                                  owner_chat_id=OWNER_CHAT, after=0) == 2
    with Session(engine) as session:
        row = session.scalar(select(Submission).where(Submission.kind == "technical"))
        assert row.payload["public_reference"] == event["draft"]["reference"]
        assert row.payload["attachments"] == event["draft"]["payload"]["attachments"]
        assert row.payload["reader_notices"]["prompt:101"]["status"] == "sent"
        assert row.payload["reader_notice_pending"] is False
        assert session.get(Submission, owner_id).status == "approved"
        assert len(session.scalars(select(TelegramUpdateReceipt)).all()) == 2
    assert [method for method, _ in bot.calls].count("sendMessage") == 1
    assert not any(method == "getUpdates" for method, _ in bot.calls)


@pytest.mark.asyncio
async def test_new_draft_matches_reference_instead_of_local_sqlite_id(engine):
    first = envelope()
    bot = OwnerBot([])
    await import_inbox(engine, bot, [first], owner_user_id=None, owner_chat_id=None)
    second = envelope(2, 502)
    second["draft"]["payload"]["text"] += " и подробности"
    second["notice"] = {"key": "accepted", "status": "uncertain"}
    second["draft"]["status"] = "new"
    for _ in range(2):
        await import_inbox(engine, bot, [second], owner_user_id=None, owner_chat_id=None, after=1)
    with Session(engine) as session:
        rows = session.scalars(select(Submission)).all()
        assert len(rows) == 1 and rows[0].status == "new"
        assert rows[0].payload["reader_notices"]["prompt:101"]["status"] == "sent"
        assert rows[0].payload["reader_notices"]["accepted"]["status"] == "uncertain"
    third = envelope(3, 503)
    with pytest.raises(ValueError, match="changed"):
        await import_inbox(engine, bot, [third], owner_user_id=None, owner_chat_id=None)
    assert bot.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["user", "chat", "topic", "source", "reference", "id"])
async def test_draft_cannot_impersonate_another_identity_or_source(engine, change):
    event = envelope()
    if change == "user":
        event["draft"]["telegram_user_id"] = 99
    elif change == "chat":
        event["update"]["message"]["chat"]["id"] = 99
    elif change == "topic":
        event["draft"]["payload"]["topic_id"] = 99
    elif change == "source":
        event["draft"]["payload"]["origin"] = "channel_direct_messages"
    elif change == "reference":
        event["draft"]["payload"]["public_reference"] = "RM-9999-9999"
    else:
        event["draft"]["id"] = 999
    with pytest.raises(ValueError):
        await import_inbox(engine, OwnerBot([]), [event], owner_user_id=None, owner_chat_id=None)
    with Session(engine) as session:
        assert session.scalar(select(Submission)) is None
        assert session.scalar(select(TelegramUpdateReceipt)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["sent", "uncertain"])
async def test_terminal_delivery_never_retries_or_loses_receipt(engine, status):
    first = envelope()
    first["notice"]["status"] = status
    bot = OwnerBot([])
    await import_inbox(engine, bot, [first], owner_user_id=None, owner_chat_id=None)
    repeat = envelope(2, 502)
    repeat["notice"]["telegram_message_id"] = 999
    with pytest.raises(ValueError, match="terminal"):
        await import_inbox(engine, bot, [repeat], owner_user_id=None, owner_chat_id=None)
    assert bot.calls == []


@pytest.mark.asyncio
async def test_legacy_seed_id_is_preserved(engine):
    with Session(engine) as session:
        row = Submission(kind="technical", status="collecting", telegram_user_id=42,
                         telegram_message_id=100, payload={"reader_intake": True, "chat_id": 42})
        session.add(row)
        session.commit()
        row_id = row.id
        seed = export_intake_state(session, datetime.now(UTC), bot_id=987)
    assert seed["bot_id"] == 987
    event = envelope()
    event["draft"].update(id=row_id, reference=str(row_id))
    event["draft"]["payload"]["public_reference"] = str(row_id)
    await import_inbox(engine, OwnerBot([]), [event], owner_user_id=None, owner_chat_id=None)
    with Session(engine) as session:
        assert session.get(Submission, row_id).payload["public_reference"] == str(row_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "active"])
async def test_canonical_source_disables_polling_and_checkpoints_cursor(
    engine, monkeypatch, phase,
):
    state = {"ingress": {**CONFIG, "phase": phase}, "serverless_sequence": 4}
    snapshots = []
    requested = []

    def peek(url, token, after):
        requested.append((url, token, after))
        return [{"sequence": 5, "update": {"update_id": 999, "message": {}}}]

    monkeypatch.setattr(intake_batch, "peek_inbox", peek)
    bot = OwnerBot([])
    result = await intake_batch.collect_batch(
        engine, bot, enabled=True, store_guard=lambda: nullcontext(True),
        run_pipeline=lambda: pytest.fail("нет новой редакционной заявки"), state=state,
        checkpoint=lambda: snapshots.append(copy.deepcopy(state)), ingress_token="token",
    )
    assert bot.calls == []
    if phase == "prepared":
        assert result["status"] == "blocked_ingress_prepared" and requested == []
        assert state["serverless_sequence"] == 4
    else:
        assert result["status"] == "ready" and snapshots[0]["serverless_sequence"] == 5
        assert requested == [(ORIGIN, "token", 4)]


@pytest.mark.asyncio
async def test_malformed_cutover_cannot_fall_back_to_polling(engine):
    with pytest.raises(ValueError):
        await intake_batch.collect_batch(
            engine, OwnerBot([]), enabled=True, store_guard=lambda: nullcontext(True),
            run_pipeline=lambda: 0, state={"ingress": {"backend": "telegram"}},
        )


@pytest.mark.parametrize("ack_fails", [False, True])
def test_runtime_purges_only_after_verified_canonical_checkpoint_and_preserves_success(
    context, tmp_path, monkeypatch, ack_fails,
):
    store, drive = context
    store.state["intake_state"] = {"ingress": CONFIG, "serverless_sequence": 4}
    receipts = []

    def child(command, env):
        assert env["EDITORIAL_INGRESS_TOKEN"] == "private-token"
        state_path = Path(env["DATABASE_URL"].removeprefix("sqlite:///")).parent
        state_path /= "intake-batch-state.json"
        value = json.loads(state_path.read_text())
        value["serverless_sequence"] = 5
        state_path.write_text(json.dumps(value))
        assert receipts == []
        return 0

    def ack(url, token, through, digest, *, open_drafts):
        assert store.state["status"] == "ready"
        assert store.state["intake_state"]["serverless_sequence"] == through == 5
        assert store.state["snapshot"]["sha256"] == digest
        assert store.state["snapshot"]["drive_file_id"] in drive.files
        assert open_drafts == []
        receipts.append((url, token, through, digest))
        if ack_fails:
            raise httpx.ConnectError("private-token")

    monkeypatch.setattr(runtime, "run_child", child)
    monkeypatch.setattr(runtime, "acknowledge_inbox", ack)
    env = {"ACTIONS_CUTOVER_CONFIRMED": "true", "GITHUB_REPOSITORY": runtime.REPO,
           "GITHUB_REF": "refs/heads/main", "EDITORIAL_INGRESS_TOKEN": "private-token"}
    assert execute(context, tmp_path, monkeypatch, mode="intake", environment=env) == 0
    assert len(receipts) == 1 and store.state["status"] == "ready"


@pytest.mark.parametrize("failure", ["upload", "cas"])
def test_failed_canonical_checkpoint_does_not_purge(context, tmp_path, monkeypatch, failure):
    store, drive = context
    store.state["intake_state"] = {"ingress": CONFIG, "serverless_sequence": 4}
    if failure == "upload":
        drive.fail_upload = True
    else:
        store.fail_commit = True

    def child(command, env):
        from sqlalchemy import create_engine

        engine = create_engine(env["DATABASE_URL"])
        with Session(engine) as session:
            session.add(Submission(kind="technical", status="new", payload={}))
            session.commit()
        engine.dispose()
        return 0

    monkeypatch.setattr(runtime, "run_child", child)
    monkeypatch.setattr(runtime, "acknowledge_inbox", lambda *a: pytest.fail("ранняя очистка"))
    with pytest.raises(ValueError):
        execute(context, tmp_path, monkeypatch, mode="intake")
    assert store.state["status"] == "busy"


def test_editorial_worker_cannot_see_ingress_token_and_preserves_cutover(
    context, tmp_path, monkeypatch,
):
    context[0].state["intake_state"] = {"ingress": CONFIG, "serverless_sequence": 0}

    def child(command, env):
        assert "EDITORIAL_INGRESS_TOKEN" not in env
        return 0

    monkeypatch.setattr(runtime, "run_child", child)
    env = {"ACTIONS_CUTOVER_CONFIRMED": "true", "GITHUB_REPOSITORY": runtime.REPO,
           "GITHUB_REF": "refs/heads/main", "EDITORIAL_INGRESS_TOKEN": "private-token"}
    assert execute(context, tmp_path, monkeypatch, environment=env) == 0
    assert context[0].state["intake_state"] == {"ingress": CONFIG, "serverless_sequence": 0}


def test_export_is_read_only_and_never_overwrites(engine, tmp_path, monkeypatch):
    database = Path(engine.url.database)
    output = tmp_path / "private-seed.json"
    before = database.read_bytes()
    monkeypatch.setattr(sys, "argv", ["serverless_snapshot", str(database), str(output),
                                      "--bot-id", "12345"])
    export_main()
    seed = json.loads(output.read_text(encoding="utf-8"))
    assert seed["version"] == 1 and seed["bot_id"] == 12345
    assert seed["form"]["routes"] and seed["form"]["menu"]
    assert database.read_bytes() == before
    with pytest.raises(FileExistsError):
        export_main()


@pytest.mark.asyncio
async def test_sequence_gap_is_rejected_before_any_receipt(engine):
    with pytest.raises(ValueError, match="sequence"):
        await import_inbox(engine, OwnerBot([]), [envelope(), envelope(3, 503)],
                           owner_user_id=None, owner_chat_id=None, after=0)
    with Session(engine) as session:
        assert session.scalar(select(TelegramUpdateReceipt)) is None


@pytest.mark.asyncio
async def test_unresolved_receipts_block_export_and_import(engine):
    with Session(engine) as session:
        session.add(TelegramUpdateReceipt(update_id=999, status="failed", kind="reader_intake"))
        session.commit()
        with pytest.raises(ValueError, match="unresolved"):
            export_intake_state(session, datetime.now(UTC))
    with pytest.raises(ValueError, match="unresolved"):
        await import_inbox(engine, OwnerBot([]), [envelope()],
                           owner_user_id=None, owner_chat_id=None)


def test_failed_ack_retries_from_next_canonical_checkpoint(context, tmp_path, monkeypatch):
    store, _ = context
    store.state["intake_state"] = {"ingress": CONFIG, "serverless_sequence": 4}
    calls = []
    monkeypatch.setattr(runtime, "run_child", lambda *a: 0)

    def ack(url, token, through, digest, *, open_drafts):
        assert store.state["status"] == "ready"
        calls.append(through)
        if len(calls) == 1:
            raise httpx.ConnectError("private-token")

    monkeypatch.setattr(runtime, "acknowledge_inbox", ack)
    for directory in ("first", "retry"):
        assert execute(context, tmp_path / directory, monkeypatch, mode="intake") == 0
    assert calls == [4, 4]


def test_intake_ai_children_do_not_inherit_ingress_token(tmp_path, monkeypatch):
    from subprocess import CompletedProcess

    monkeypatch.setenv("EDITORIAL_INGRESS_TOKEN", "private-token")

    def run(command, **options):
        assert "EDITORIAL_INGRESS_TOKEN" not in options["env"]
        return CompletedProcess(command, 0)

    class Child:
        args = ["owner-review"]
        returncode = 0

        def __init__(self, command, **options):
            assert "EDITORIAL_INGRESS_TOKEN" not in options["env"]

        def communicate(self, **options):
            return '{"status":"ready"}', ""

    monkeypatch.setattr(intake_batch.subprocess, "run", run)
    monkeypatch.setattr(intake_batch.subprocess, "Popen", Child)
    assert intake_batch._run_actions_submission_pipeline(tmp_path) == 0
    assert intake_batch._run_owner_review() == (0, {"status": "ready"})


def test_ack_routes_only_include_open_canonical_drafts_without_content(engine):
    with Session(engine) as session:
        for status in ("choosing", "collecting", "new", "cancelled"):
            session.add(Submission(
                kind="technical", status=status, telegram_user_id=42, telegram_message_id=101,
                payload={"reader_intake": True, "public_reference": f"RM-{status}",
                         "chat_id": 42, "text": "PRIVATE", "attachments": [{"file_id": "SECRET"}]},
            ))
        session.commit()
    routes = open_draft_routes(Path(engine.url.database))
    assert routes == [
        {"reference": f"RM-{status}", "telegram_user_id": 42, "chat_id": 42, "topic_id": None}
        for status in ("choosing", "collecting")
    ]
    assert "PRIVATE" not in json.dumps(routes) and "SECRET" not in json.dumps(routes)


def test_ack_with_open_draft_routes_uses_same_private_post():
    routes = [{"reference": "RM-1234-5678", "telegram_user_id": 42,
               "chat_id": 42, "topic_id": None}]

    def handler(request):
        assert json.loads(request.content)["open_drafts"] == routes
        return httpx.Response(200, json={"status": "ready"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        vercel_inbox.acknowledge_inbox(ORIGIN, "token", 4, "b" * 64,
                                      open_drafts=routes, client=client)


def test_unreadable_saved_routes_defer_ack_without_undoing_ready(
    context, tmp_path, monkeypatch,
):
    store, _ = context
    store.state["intake_state"] = {"ingress": CONFIG, "serverless_sequence": 4}
    monkeypatch.setattr(runtime, "run_child", lambda *a: 0)

    def unreadable(database):
        assert store.state["status"] == "ready"
        raise OSError("private routing state")

    monkeypatch.setattr(runtime, "open_draft_routes", unreadable)
    monkeypatch.setattr(runtime, "acknowledge_inbox", lambda *a, **k: pytest.fail("ранняя очистка"))
    assert execute(context, tmp_path, monkeypatch, mode="intake") == 0
    assert store.state["status"] == "ready"
