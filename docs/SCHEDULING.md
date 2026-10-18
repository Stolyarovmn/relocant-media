# Operational scheduling

`relocant-schedule` is deliberately a short-lived deterministic command. It does not run an in-process scheduler and it has no authority to publish Telegram posts or change advertising budgets.

## Tick

Run one scheduling pass:

```bash
relocant-schedule tick \
  --city-config cities/belgrade/city.yaml \
  --sources cities/belgrade/sources
```

The command may be invoked every minute. Each tick:

1. loads the city timezone and source registry;
2. runs only sources whose `check_every` interval is due;
3. identifies published posts whose 1h/6h/24h/72h metric snapshot is due;
4. creates the previous local day's daily report after `operations.daily_report_local_time`;
5. persists every attempted task in `scheduler_run`.

The default snapshot provider is intentionally unavailable until the dedicated MTProto analytics adapter is configured. Due snapshot runs are recorded as `blocked` and retried with backoff; no zero metrics are fabricated.

## Concurrency and restart behaviour

`scheduler_lease` holds a PostgreSQL lease per task key. Claim uses a conditional `INSERT .. ON CONFLICT DO UPDATE`, so two cron processes cannot intentionally run the same source/snapshot/report task at the same time. A crashed process leaves only a time-bounded lease, allowing later recovery.

`scheduler_run` records task type, city, due timestamp, attempt, status, timestamps, safe error information and task details. A successful recurring source is not due again until its configured interval has elapsed. Failed/blocked tasks use exponential retry delays of 1, 2, 4, 8, 16, 32 and then 60 minutes.

## Status

```bash
relocant-schedule status --city belgrade --limit 50
```

This is the first-line operational audit. Collector-specific failures are additionally written to `collector_failure`.

## cron example

Run the scheduler once per minute on the staging host:

```cron
* * * * * cd /srv/relocant-media && docker compose --profile staging exec -T api relocant-schedule tick --city-config cities/belgrade/city.yaml --sources cities/belgrade/sources >> /var/log/relocant-scheduler.log 2>&1
```

Use an external lock only if the host scheduler requires it for log hygiene; PostgreSQL leases remain the correctness mechanism.

## systemd timer example

Service command:

```text
ExecStart=/usr/bin/docker compose --profile staging exec -T api relocant-schedule tick --city-config cities/belgrade/city.yaml --sources cities/belgrade/sources
```

Configure the timer with `OnCalendar=*-*-* *:*:00` and `Persistent=true` if systemd should trigger promptly after host downtime. Persistent scheduler state in PostgreSQL still decides what work is actually due.

## Kubernetes CronJob

Use the same container image and command on a one-minute CronJob with `concurrencyPolicy: Forbid`. Database leases remain enabled because multiple schedulers or manual invocations may still overlap.

## Daily report timing

The city config owns the local reporting time:

```yaml
operations:
  daily_report_local_time: "00:15"
```

At/after that local time the scheduler stores one report for the previous local calendar day. If the service was down, subsequent ticks advance missing report days one at a time instead of silently duplicating them.

## Safety boundary

Scheduled work may ingest sources, collect observed analytics and build reports. It must not:

- publish a Telegram post;
- accept an advertisement;
- change campaign bids or budgets;
- bypass editorial verification;
- synthesize missing analytics values.

Those remain explicit runtime/owner-controlled actions.
