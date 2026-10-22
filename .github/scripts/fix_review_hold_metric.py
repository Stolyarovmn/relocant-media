from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "src/relocant_media/ops/server_cycle.py"
CI = ROOT / ".github/workflows/ci.yml"
SELF = Path(__file__).resolve()
BRANCH = "feat/undated-venue-review-holds"

text = SERVER.read_text(encoding="utf-8")
old = '    result["venue_sources"] = len(runtime_venues)\n'
new = '    result["venue_sources"] = len(venues)\n'
if text.count(old) != 1:
    raise RuntimeError(f"expected one venue metric assignment, got {text.count(old)}")
SERVER.write_text(text.replace(old, new, 1), encoding="utf-8")

subprocess.run(["git", "fetch", "origin", "main"], cwd=ROOT, check=True)
CI.write_text(
    subprocess.check_output(
        ["git", "show", "origin/main:.github/workflows/ci.yml"], cwd=ROOT, text=True
    ),
    encoding="utf-8",
)
SELF.unlink()
subprocess.run(["git", "config", "user.name", "IGT Runtime Patch"], cwd=ROOT, check=True)
subprocess.run(
    ["git", "config", "user.email", "runtime-patch@users.noreply.github.com"],
    cwd=ROOT,
    check=True,
)
subprocess.run(["git", "add", "-A"], cwd=ROOT, check=True)
subprocess.run(["git", "commit", "-m", "Preserve dated venue source metric"], cwd=ROOT, check=True)
subprocess.run(["git", "push", "origin", f"HEAD:refs/heads/{BRANCH}"], cwd=ROOT, check=True)
