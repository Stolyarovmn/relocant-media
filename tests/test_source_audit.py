from typing import Any

from relocant_media.ops.source_audit import (
    render_source_health_markdown,
    source_health_exit_code,
)


def _result(
    *,
    active: int = 1,
    errors: int = 0,
    partial: int = 0,
    transport_errors: int = 0,
) -> dict[str, Any]:
    return {
        "checked_at": "2026-10-21T22:06:15+00:00",
        "active_count": active,
        "working_count": max(active - errors - transport_errors, 0),
        "error_count": errors,
        "partial_count": partial,
        "transport_error_count": transport_errors,
        "sources": [],
    }


def test_source_health_exit_code_accepts_healthy_registry() -> None:
    assert (
        source_health_exit_code(
            _result(),
            fail_on_error=True,
            fail_on_partial=True,
            fail_on_transport=True,
            require_active=True,
        )
        == 0
    )


def test_source_health_exit_code_rejects_live_errors() -> None:
    assert source_health_exit_code(_result(errors=1), fail_on_error=True) == 1


def test_source_health_exit_code_can_reject_partial_extraction() -> None:
    assert source_health_exit_code(_result(partial=1), fail_on_partial=True) == 1


def test_source_health_exit_code_separates_transport_policy() -> None:
    result = _result(transport_errors=1)

    assert source_health_exit_code(result, fail_on_error=True) == 0
    assert source_health_exit_code(result, fail_on_transport=True) == 1


def test_source_health_exit_code_rejects_empty_active_registry_when_required() -> None:
    assert source_health_exit_code(_result(active=0), require_active=True) == 1


def test_source_health_exit_code_keeps_non_strict_cli_compatible() -> None:
    assert (
        source_health_exit_code(
            _result(active=0, errors=2, partial=3, transport_errors=1)
        )
        == 0
    )


def test_render_source_health_markdown_exposes_degraded_sources() -> None:
    result = _result(active=4, errors=1, partial=1, transport_errors=1)
    result["sources"] = [
        {"id": "good", "status": "ok", "fresh_items": 2, "attempts": 1},
        {
            "id": "partial",
            "status": "partial",
            "fresh_items": 1,
            "item_failures": [{"url": "https://example.test/a", "error_type": "ValueError"}],
        },
        {
            "id": "network",
            "status": "transport_error",
            "error_type": "ConnectError",
            "attempts": 3,
        },
        {"id": "broken", "status": "error", "error_type": "ValueError"},
        {"id": "future", "status": "planned"},
    ]

    rendered = render_source_health_markdown(result)

    assert "partial: **1**" in rendered
    assert "transport errors: **1**" in rendered
    assert "extraction/protocol errors: **1**" in rendered
    assert "| partial | partial | 1 | item_failures=1 |" in rendered
    assert "| network | transport_error | - | ConnectError after 3 attempts |" in rendered
    assert "| broken | error | - | ValueError |" in rendered
    assert "future" not in rendered
