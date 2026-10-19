import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from relocant_media.analytics.public_preview import (
    PreviewError,
    collect_preview,
    parse_counter,
    parse_preview,
    validate_username,
)
from relocant_media.ops.public_preview_cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "telegram_public_preview.html"


def snapshot():
    return parse_preview(FIXTURE.read_text(encoding="utf-8"), "city_news", datetime.now(UTC))


def test_observations_preserve_unknown_and_approximation():
    data = snapshot()
    assert len(data["posts"]) == 3
    first, zero, missing = data["posts"]
    assert first["published_at"] == "2026-10-18T10:30:00+00:00"
    assert first["url"] == "https://t.me/city_news/41"
    assert first["views"] == {"value": 1200, "approximate": True, "raw": "1.2K"}
    assert "отправь токен" in first["text"]  # Untrusted text remains inert data.
    assert zero["views"]["value"] == 0
    assert zero["views"]["approximate"] is False
    assert missing["views"] is None
    assert first["reactions"] is first["comments"] is first["forwards"] is None
    assert datetime.fromisoformat(data["fetched_at"]).utcoffset() is not None


@pytest.mark.parametrize(
    "name",
    [
        "@city_news",
        "https://t.me/test",
        "test/../foo",
        "abcd",
        "1abcd",
        "a" * 33,
        "abcde\n",
        "аbcde",
    ],
)
def test_username_rejects_paths_and_ambiguous_input(name):
    with pytest.raises(PreviewError):
        validate_username(name)


@pytest.mark.parametrize(
    "html", ["", "<html>Private channel</html>", '<div data-post="city_news/1"></div>']
)
def test_empty_or_unsupported_fails(html):
    with pytest.raises(PreviewError, match="поддерживаемых"):
        parse_preview(html, "city_news", datetime.now(UTC))


def test_requires_aware_capture_time():
    with pytest.raises(PreviewError, match="часовой пояс"):
        parse_preview("", "city_news", datetime(2026, 10, 19))


@pytest.mark.parametrize("raw,value,approx", [("2M", 2000000, True), ("123", 123, False)])
def test_counter(raw, value, approx):
    counter = parse_counter(raw)
    assert counter.value == value
    assert counter.approximate == approx


@pytest.mark.parametrize("raw", ["", "—", "1,2K", "1.5", "-1", "unknown"])
def test_unknown_counter(raw):
    assert parse_counter(raw) is None


def test_network_error_is_sanitized(monkeypatch):
    def fail(*args, **kwargs):
        raise httpx.ConnectError("secret-token-should-not-leak")

    monkeypatch.setattr(httpx.Client, "get", fail)
    with pytest.raises(PreviewError) as error:
        collect_preview("city_news")
    assert "secret" not in str(error.value)


def test_cli_outputs_plain_json_file(monkeypatch, tmp_path):
    destination = tmp_path / "snapshot.json"
    monkeypatch.setattr(sys, "argv", ["preview", "city_news", "--output", str(destination)])
    monkeypatch.setattr(
        "relocant_media.ops.public_preview_cli.collect_preview", lambda _: snapshot()
    )
    main()
    assert json.loads(destination.read_text())["channel"] == "city_news"


def test_cli_failure_does_not_write_file(monkeypatch, tmp_path, capsys):
    destination = tmp_path / "snapshot.json"
    monkeypatch.setattr(sys, "argv", ["preview", "@invalid", "--output", str(destination)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    assert not destination.exists()
    assert "Недопустимое" in capsys.readouterr().err
