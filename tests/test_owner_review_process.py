import os
import subprocess

import pytest

from relocant_media.ops import intake_batch as batch


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("worker", 900), KeyboardInterrupt()])
def test_timeout_or_interrupt_reaps_child_before_returning(monkeypatch, failure):
    events = []
    class Child:
        args = ["worker"]
        pid = 12345
        returncode = -9
        def communicate(self, timeout=None):
            events.append(("communicate", timeout))
            if timeout is not None:
                raise failure
            return "PRIVATE OUTPUT", "SECRET\nowner_review_phase=actions\nowner_review_phase=SECRET"
        def kill(self):
            events.append("kill")
    def popen(*args, **kwargs):
        assert kwargs["start_new_session"] is True
        return Child()
    monkeypatch.setattr(batch.subprocess, "Popen", popen)
    monkeypatch.setattr(batch.os, "killpg", lambda *a: events.append("killpg"), raising=False)
    if isinstance(failure, KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            batch._run_owner_review()
    else:
        code, result = batch._run_owner_review()
        assert code == 1
        assert result == {"status": "failed", "error_type": "owner_review_timeout",
                          "phase": "actions"}
    assert events == [("communicate", 900), "killpg" if os.name == "posix" else "kill",
                      ("communicate", None)]
