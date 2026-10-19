import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_editorial_integration import editorial_session as _editorial_session

from relocant_media.ops.candidates_cli import (
    aware_datetime,
    evidence_sha256,
    export_candidates,
    select_candidates,
)
from relocant_media.storage.models import EditorialRun, RawItem, Source

editorial_session = _editorial_session
NOW = datetime(2026, 10, 19, tzinfo=UTC)


def seed(session: Session) -> None:
    session.add_all([
        Source(id=key, city=city, name=key, url=f"https://example.org/{key}",
               source_type="official", trust=1, enabled=enabled)
        for key, city, enabled in [
            ("a", "belgrade", True), ("b", "belgrade", False), ("c", "other", True),
        ]
    ])
    session.flush()
    for external_id, source_id, date, url in [
        ("first", "a", NOW, "first"),
        ("second", "a", NOW + timedelta(hours=1), "second"),
        ("until", "a", NOW + timedelta(days=1), "until"),
        ("old", "a", NOW - timedelta(seconds=1), "old"),
        ("missing", "a", None, "missing"),
        ("disabled", "b", NOW, "disabled"),
        ("other", "c", NOW, "other"),
        ("reviewed", "a", NOW, "reviewed"),
        ("duplicate", "a", NOW, "reviewed"),
    ]:
        session.add(RawItem(
            source_id=source_id,
            external_id=external_id,
            url=f"https://example.org/{url}",
            title=f"Точный заголовок {url}",
            body=f"Точный\nтекст с пробелами  {url}",
            published_at=date,
        ))
    session.flush()
    reviewed = session.scalar(select(RawItem.id).where(RawItem.external_id == "reviewed"))
    assert reviewed is not None
    session.add(EditorialRun(raw_item_id=reviewed, outcome={}, evidence={},
                             delivery_status="pending"))
    session.commit()


def test_export_selects_exact_window_and_excludes_reviewed_url(editorial_session: Session) -> None:
    seed(editorial_session)
    result = select_candidates(editorial_session, city="belgrade", since=NOW,
                               until=NOW + timedelta(days=1), limit=1)
    assert result["eligible_count"] == 2 and result["truncated"] is True
    assert [item["article_id"] for item in result["candidates"]] == ["first"]
    evidence = result["candidates"][0]["evidence"]
    assert evidence["body"] == "Точный\nтекст с пробелами  first"
    assert result["candidates"][0]["evidence_sha256"] == evidence_sha256(evidence)
    assert result["candidates"][0]["event"]["member_count"] == 1
    assert result["missing_published_at_count"] == 1
    assert result["excluded_missing_published_at"][0]["article_id"] == "missing"
    assert editorial_session.scalar(select(func.count()).select_from(EditorialRun)) == 1


def test_export_manifest_without_loading_runtime(
    editorial_session: Session, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import builtins
    import os

    seed(editorial_session)
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.startswith(("relocant_media.config", "relocant_media.ai", "relocant_media.bot")):
            raise AssertionError("Экспорт не должен загружать настройки, AI или Telegram")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    output = tmp_path / "new"
    result = export_candidates(city="belgrade", since=NOW, until=NOW + timedelta(days=1),
                               limit=50, output=output)
    assert json.loads((output / "manifest.json").read_text(encoding="utf-8")) == result
    assert result["candidate_count"] == 2
    with pytest.raises(ValueError, match="существует"):
        export_candidates(city="belgrade", since=NOW, until=NOW + timedelta(days=1),
                          limit=50, output=output)


def test_unreviewed_urls_are_deduplicated_before_count_and_limit(
    editorial_session: Session,
) -> None:
    seed(editorial_session)
    for external_id, published_at in [
        ("same-time-copy", NOW), ("later-copy", NOW + timedelta(hours=2)),
    ]:
        editorial_session.add(RawItem(
            source_id="a", external_id=external_id, url="https://example.org/first",
            title="Другой заголовок", body="Другой текст", published_at=published_at,
        ))
    editorial_session.commit()
    result = select_candidates(editorial_session, city="belgrade", since=NOW,
                               until=NOW + timedelta(days=1), limit=2)
    assert result["eligible_count"] == result["candidate_count"] == 2
    assert result["truncated"] is False
    assert [item["article_id"] for item in result["candidates"]] == ["first", "second"]


def test_reviewed_event_from_another_source_does_not_reenter_queue(
    editorial_session: Session,
) -> None:
    seed(editorial_session)
    editorial_session.add(
        Source(
            id="d",
            city="belgrade",
            name="d",
            url="https://example.org/d",
            source_type="media",
            trust=1,
            enabled=True,
        )
    )
    editorial_session.flush()
    editorial_session.add(
        RawItem(
            source_id="d",
            external_id="reviewed-from-d",
            url="https://example.org/d/reviewed-story",
            title="Точный заголовок reviewed",
            body="Независимый пересказ того же события",
            published_at=NOW + timedelta(hours=2),
        )
    )
    editorial_session.commit()

    result = select_candidates(
        editorial_session,
        city="belgrade",
        since=NOW,
        until=NOW + timedelta(days=1),
        limit=50,
    )

    assert "reviewed-from-d" not in {
        item["article_id"] for item in result["candidates"]
    }


@pytest.mark.parametrize("environment", ["production", "prod", "development", ""])
def test_environment_rejected_before_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, environment: str,
) -> None:
    from relocant_media.ops import candidates_cli

    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setattr(candidates_cli, "create_engine",
                        lambda *a, **kw: pytest.fail("БД не должна открываться"))
    with pytest.raises(ValueError, match="staging"):
        export_candidates(city="belgrade", since=NOW, until=NOW + timedelta(days=1),
                          limit=5, output=tmp_path / "new")


@pytest.mark.parametrize("limit", [0, 51])
def test_invalid_window_limit_and_timezone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, limit: int,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")
    with pytest.raises(ValueError):
        export_candidates(city="belgrade", since=NOW, until=NOW,
                          limit=limit, output=tmp_path / "new")
    with pytest.raises(ValueError):
        aware_datetime("2026-10-19T00:00:00")
