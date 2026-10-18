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
3. samples channel subscribers according to `operations.channel_snapshot_every`;
4. identifies published posts whose 1h/6h/24h/72h metric snapshot is due;
5. creates the previous local day's daily report after `operations.daily_report_local_time`;
6. persists every attempted task in `scheduler_run`.

When MTProto analytics is disabled or unavailable, channel/post analytics tasks are recorded as `blocked` and retried with backoff. No zero metrics are fabricated. Source ingestion and daily-report work continue.

## Concurrency and restart behaviour

`scheduler_lease` holds a PostgreSQL lease per task key. Claim uses a conditional `INSERT .. ON CONFLICT DO UPDATE`, so two cron processes cannot intentionally run the same source/snapshot/report task at the same time. A crashed process leaves only a time-bounded lease, allowing later recovery.

`scheduler_run` records task type, city, due timestamp, attempt, status, timestamps, safe error information and task details. A successful recurring source is not due again until its configured interval has elapsed. Failed/blocked tasks use exponential retry delays of 1, 2, 4, 8, 16, 32 and then 60 minutes.

## Status

```bash
relocant-schedule status --city belgrade --limit 50
```

This is the first-line operational audit. Collector-specific failures are additionally written to `collector_failure`.

## Runtime choices

The API container intentionally does not receive the personal MTProto session. Therefore:

- running `relocant-schedule` in the `api` service performs ingestion/reporting but analytics snapshots will be blocked;
- running it in the isolated `analytics` service enables MTProto channel/post observations after owner authorization.

See `docs/MTPROTO_ANALYTICS.md` for the one-time authorization flow.

## cron example with MTProto analytics

Run a short-lived isolated analytics container once per minute:

```cron
* * * * * cd /srv/relocant-media && docker compose --profile analytics run --rm -T analytics relocant-schedule tick --city-config cities/belgrade/city.yaml --sources cities/belgrade/sources >> /var/log/relocant-scheduler.log 2>&1
```

The named `telegram_analytics_session` volume persists the authorized session between invocations. PostgreSQL leases remain the correctness mechanism if ticks overlap.

If MTProto has not been authorized yet, the same command remains safe: Telegram analytics tasks become `blocked` while source ingestion and reporting continue.

## systemd timer example

Service command:

```text
ExecStart=/usr/bin/docker compose --profile analytics run --rm -T analytics relocant-schedule tick --city-config cities/belgrade/city.yaml --sources cities/belgrade/sources
```

Configure the timer with `OnCalendar=*-*-* *:*:00` and `Persistent=true` if systemd should trigger promptly after host downtime. Persistent scheduler state in PostgreSQL still decides what work is actually due.

## Kubernetes CronJob

Use the same container image and command on a one-minute CronJob with `concurrencyPolicy: Forbid`. Mount the MTProto session as an isolated secret/persistent volume available only to the analytics CronJob. Database leases remain enabled because multiple schedulers or manual invocations may still overlap.

## Daily report and channel snapshot timing

The city config owns local reporting and channel observation cadence:

```yaml
operations:
  daily_report_local_time: "00:15"
  channel_snapshot_every: "1h"
```

At/after the report time the scheduler stores one report for the previous local calendar day. If the service was down, subsequent ticks advance missing report days one at a time instead of silently duplicating them.

## Safety boundary

Scheduled work may ingest sources, collect observed analytics and build reports. It must not:

- publish a Telegram post;
- accept an advertisement;
- change campaign bids or budgets;
- bypass editorial verification;
- synthesize missing analytics values.

Those remain explicit runtime/owner-controlled actions.
