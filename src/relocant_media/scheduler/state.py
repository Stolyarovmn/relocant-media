from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from relocant_media.scheduler.models import SchedulerLease, SchedulerRun
from relocant_media.scheduler.timing import retry_backoff


def claim_lease(
    session: Session,
    *,
    task_key: str,
    now: datetime,
    lease_for: timedelta = timedelta(minutes=15),
) -> bool:
    lease_until = now + lease_for
    statement = insert(SchedulerLease).values(
        task_key=task_key,
        lease_until=lease_until,
        updated_at=now,
    )
    statement = statement.on_conflict_do_update(
        index_elements=["task_key"],
        set_={"lease_until": lease_until, "updated_at": now},
        where=SchedulerLease.lease_until <= now,
    ).returning(SchedulerLease.task_key)
    claimed = session.scalar(statement) is not None
    session.commit()
    return claimed


def release_lease(session: Session, *, task_key: str, now: datetime) -> None:
    session.execute(
        update(SchedulerLease)
        .where(SchedulerLease.task_key == task_key)
        .values(lease_until=now, updated_at=now)
    )
    session.commit()


def latest_run(session: Session, task_key: str) -> SchedulerRun | None:
    return session.scalar(
        select(SchedulerRun)
        .where(SchedulerRun.task_key == task_key)
        .order_by(SchedulerRun.started_at.desc(), SchedulerRun.id.desc())
        .limit(1)
    )


def retry_is_due(run: SchedulerRun | None, now: datetime) -> bool:
    if run is None:
        return True
    if run.status == "success":
        return False
    if run.status == "running":
        return False
    if run.finished_at is None:
        return True
    return run.finished_at + retry_backoff(run.attempt) <= now


def start_run(
    session: Session,
    *,
    task_key: str,
    task_type: str,
    city: str,
    scheduled_for: datetime,
    now: datetime,
) -> int:
    last_attempt = session.scalar(
        select(func.max(SchedulerRun.attempt)).where(
            SchedulerRun.task_key == task_key,
            SchedulerRun.scheduled_for == scheduled_for,
        )
    )
    run = SchedulerRun(
        task_key=task_key,
        task_type=task_type,
        city=city,
        scheduled_for=scheduled_for,
        attempt=int(last_attempt or 0) + 1,
        status="running",
        started_at=now,
        details={},
    )
    session.add(run)
    session.commit()
    return run.id


def finish_run(
    session: Session,
    *,
    run_id: int,
    status: str,
    now: datetime,
    details: dict[str, object] | None = None,
    error: Exception | None = None,
) -> None:
    run = session.get(SchedulerRun, run_id)
    if run is None:
        raise RuntimeError(f"scheduler run {run_id} disappeared")
    run.status = status
    run.finished_at = now
    run.details = details or {}
    if error is not None:
        run.error_type = type(error).__name__
        run.error_message = str(error)[:4000]
    else:
        run.error_type = None
        run.error_message = None
    session.commit()
