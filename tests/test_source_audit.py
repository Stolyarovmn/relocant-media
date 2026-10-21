from typing import Any

from relocant_media.ops.source_audit import source_health_exit_code


def _result(*, active: int = 1, errors: int = 0, partial: int = 0) -> dict[str, Any]:
    return {
        "active_count": active,
        "error_count": errors,
        "partial_count": partial,
    }


def test_source_health_exit_code_accepts_healthy_registry() -> None:
    assert (
        source_health_exit_code(
            _result(),
            fail_on_error=True,
            fail_on_partial=True,
            require_active=True,
        )
        == 0
    )


def test_source_health_exit_code_rejects_live_errors() -> None:
    assert source_health_exit_code(_result(errors=1), fail_on_error=True) == 1


def test_source_health_exit_code_can_reject_partial_extraction() -> None:
    assert source_health_exit_code(_result(partial=1), fail_on_partial=True) == 1


def test_source_health_exit_code_rejects_empty_active_registry_when_required() -> None:
    assert source_health_exit_code(_result(active=0), require_active=True) == 1


def test_source_health_exit_code_keeps_non_strict_cli_compatible() -> None:
    assert source_health_exit_code(_result(active=0, errors=2, partial=3)) == 0
