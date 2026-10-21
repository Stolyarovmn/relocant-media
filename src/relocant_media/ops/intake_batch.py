"""Five-minute private intake batches; one serialized production pass, never per message."""
import asyncio
import json
import os
import subprocess
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from relocant_media.bot.api import TelegramBotAPI
from relocant_media.ops.sqlite_backup_cli import create_snapshot
from relocant_media.services.private_intake import receive_private_tips
from relocant_media.services.submission_queue import batch_submissions
from relocant_media.storage.file_lock import file_lock
from relocant_media.storage.sqlite_schema import verify_sqlite_schema


async def collect_batch(
    engine: Engine, bot: TelegramBotAPI, *, enabled: bool,
    store_guard: Callable[[], AbstractContextManager[bool]],
    run_pipeline: Callable[[], int], state: dict[str, Any],
    checkpoint: Callable[[], None] | None = None,
) -> dict[str, Any]:
    intake = await receive_private_tips(engine,bot,enabled=enabled,
                                       poll_timeout=1,store_guard=store_guard)
    result: dict[str, Any] = dict(status=intake["status"],intake=intake,pipeline_started=False)
    if intake["status"] not in {"ready", "busy"}:
        return result
    with Session(engine) as session:
        ids = [row.id for row in batch_submissions(session)]
    result["pending_submissions"] = len(ids)
    if not ids:
        return result
    signature = ",".join(map(str,ids))
    attempts = state.get("attempts",0) if state.get("signature") == signature else 0
    if attempts >= 3:
        result["status"] = "blocked_retry_budget"
        return result
    state.update(signature=signature,attempts=attempts+1)
    if checkpoint is not None:
        checkpoint()
    result["pipeline_started"] = True
    code = await asyncio.to_thread(run_pipeline)
    result.update(status="ready" if code == 0 else "pipeline_failed",pipeline_exit_code=code)
    return result


def main() -> None:
    if os.environ.get("EDITORIAL_INTAKE_MODE") != "batch":
        raise SystemExit("Требуется EDITORIAL_INTAKE_MODE=batch")
    url = make_url(os.environ["DATABASE_URL"])
    if (url.get_backend_name() != "sqlite" or not url.database or url.query
            or not Path(url.database).is_file()):
        raise ValueError("existing persistent SQLite required")
    directory = Path(url.database).parent
    state_path = directory / "intake-batch-state.json"
    with file_lock(directory/"intake-consumer.lock") as acquired:
        if not acquired:
            raise SystemExit("Другой приёмник уже работает")
        state = json.loads(state_path.read_text()) if state_path.is_file() else {}
        def checkpoint() -> None:
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
            temporary.replace(state_path)
        engine = create_engine(url)
        verify_sqlite_schema(engine)
        def pipeline() -> int:
            # No shell interpolation, no secret output. The child uses the same worker.lock.
            completed = subprocess.run(
                ["/opt/relocant-media/app/deploy/server/run-cycle.sh", "--submissions-only"],
                capture_output=True, timeout=4100, check=False)
            return completed.returncode
        async def run() -> dict[str, Any]:
            async with httpx.AsyncClient(timeout=25,trust_env=False) as client:
                return await collect_batch(engine,
                    TelegramBotAPI(os.environ["TELEGRAM_BOT_TOKEN"],client),
                    enabled=os.environ.get("EDITORIAL_PRIVATE_INTAKE_ENABLED") == "true",
                    store_guard=lambda: file_lock(directory/"worker.lock"),
                    run_pipeline=pipeline,state=state,checkpoint=checkpoint)
        try:
            result = asyncio.run(run())
        except Exception as error:
            result = dict(status="failed",error_type=type(error).__name__)
        finally:
            engine.dispose()
        state.update(last_result=result,checked_at=datetime.now(UTC).isoformat())
        if result.get("intake", {}).get("inspected", 0) or result.get("pipeline_started"):
            state["backup_pending"] = True
        checkpoint()
        if state.get("backup_pending"):
            snapshot_path = directory / "intake-snapshots" / datetime.now(UTC).strftime(
                "%Y%m%dT%H%M%S.%fZ")
            try:
                snapshot = create_snapshot(Path(url.database), snapshot_path)
            except Exception as error:
                result.update(status="failed",backup_error_type=type(error).__name__)
            else:
                state["backup_pending"] = False
                result.update(backup_ready=True,backup_sha256=snapshot["sha256"],
                              remote_backup="pending",snapshot_path=str(snapshot_path))
        checkpoint()
        print(json.dumps(result,ensure_ascii=False))
        if result["status"] not in {"ready", "busy", "disabled"}:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
