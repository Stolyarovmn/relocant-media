import json

import pytest

from relocant_media.ops import owner_review_cycle


def test_cli_reports_failure_without_secret_exception_values(monkeypatch, capsys):
    def fail():
        raise PermissionError("private-token=https://example.test/secret-owner-data")

    monkeypatch.setattr(owner_review_cycle, "main", fail)
    with pytest.raises(SystemExit) as raised:
        owner_review_cycle.run_cli()
    assert raised.value.code == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["status"] == "failed"
    assert result["error_type"] == "PermissionError"
    assert result["failure_location"]["function"] == "fail"
    assert result["failure_location"]["module"] == "test_owner_review_diagnostics.py"
    assert result["failure_location"]["line"] > 0
    assert "private-token" not in captured.out + captured.err
    assert "secret-owner-data" not in captured.out + captured.err
    assert captured.err == ""


def test_cli_preserves_success_output(monkeypatch, capsys):
    monkeypatch.setattr(owner_review_cycle, "main", lambda: print('{"status":"ready"}'))
    owner_review_cycle.run_cli()
    assert json.loads(capsys.readouterr().out) == {"status": "ready"}
