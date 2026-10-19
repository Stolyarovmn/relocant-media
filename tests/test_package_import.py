from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session
from test_editorial_integration import prepare_service
from test_editorial_package import NOW, bind_context, package_payload, parse

from relocant_media.services.package_import import PackageConflictError, import_package
from relocant_media.storage.models import RawItem

editorial_session = _editorial_session


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["dry-run", "fake"])
async def test_package_cli_preview_delivery_without_ai_or_real_network(
    editorial_session: Session, monkeypatch: pytest.MonkeyPatch, transport: str,
) -> None:
    import builtins

    import httpx

    from relocant_media.ops.package_cli import _persist
    from relocant_media.storage import db
    from relocant_media.storage.models import EditorialRun

    _, provider, item_id = prepare_service(editorial_session)
    payload = package_payload()
    payload["raw_item_id"] = item_id
    for review in payload["reviews"]:
        review["reviewed_at"] = datetime.now(UTC).isoformat()
    bind_context(payload)
    package = parse(payload)
    monkeypatch.setattr(db, "SessionFactory", lambda: editorial_session)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "relocant_media.ai.gemini":
            raise AssertionError("Gemini must not be initialized")
        return original_import(name, *args, **kwargs)

    async def forbidden_network(*args, **kwargs):
        raise AssertionError("real HTTP transport must not be called")

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden_network)
    first = await _persist(package, transport)
    second = await _persist(package, transport)
    assert first == second
    assert first["delivery"]["status"] == "dry_run"
    if transport == "fake":
        requests = first["fake_requests"]
        assert len(requests) == 1
        assert requests[0]["chat_id"] == "-100000000001"
        assert requests[0]["text"] == first["delivery"]["text"]
    else:
        assert "fake_requests" not in first
    run = editorial_session.get(EditorialRun, item_id)
    assert run is not None
    assert run.delivery_status == "pending" and run.staging_message_id is None
    assert provider.calls == []


def test_package_import_is_idempotent_and_rejects_new_draft(editorial_session: Session) -> None:
    _, provider, item_id = prepare_service(editorial_session)
    payload = package_payload()
    payload["raw_item_id"] = item_id
    bind_context(payload)
    package = parse(payload)
    run = import_package(editorial_session, package, now=NOW)
    assert run.outcome["status"] == "ready" and run.post_id is not None
    assert run.delivery_status == "pending" and run.staging_message_id is None
    assert import_package(editorial_session, package, now=NOW).post_id == run.post_id
    assert provider.calls == []
    payload["reviews"][0]["score"] = 81
    with pytest.raises(PackageConflictError):
        import_package(editorial_session, parse(payload), now=NOW)


def test_changed_raw_evidence_rejects_package(editorial_session: Session) -> None:
    _, _, item_id = prepare_service(editorial_session)
    item = editorial_session.get(RawItem, item_id)
    assert item is not None
    item.body += " updated"
    editorial_session.commit()
    payload = package_payload()
    payload["raw_item_id"] = item_id
    bind_context(payload)
    with pytest.raises(PackageConflictError, match="evidence changed"):
        import_package(editorial_session, parse(payload), now=NOW)


@pytest.mark.asyncio
async def test_persisted_package_reviews_expire_before_staging_delivery(
    editorial_session: Session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from relocant_media.ai import package as package_module
    from relocant_media.services.editorial import DeliveryBlockedError

    service, _, item_id = prepare_service(editorial_session)
    payload = package_payload()
    payload["raw_item_id"] = item_id
    bind_context(payload)
    import_package(editorial_session, parse(payload), now=NOW)
    evaluate = package_module.evaluate_package
    monkeypatch.setattr(package_module, "evaluate_package",
                        lambda package: evaluate(package, now=NOW + timedelta(hours=25)))
    with pytest.raises(DeliveryBlockedError, match="human_review"):
        await service.deliver(item_id)
