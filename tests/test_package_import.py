from datetime import timedelta

import pytest
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session
from test_editorial_integration import prepare_service
from test_editorial_package import NOW, bind_context, package_payload, parse

from relocant_media.services.package_import import PackageConflictError, import_package
from relocant_media.storage.models import RawItem

editorial_session = _editorial_session


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
