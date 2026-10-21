# Source yield analytics

`relocant-source-yield` is a read-only report for measuring how much useful newsroom output each
registered source actually produces.

## Funnel

For every source in a city the report shows:

- `found` — raw items ingested during the requested window;
- `editorial_runs` — those raw items that became an `EditorialRun`;
- `post_created` — runs that produced a `Post`;
- `ready` — produced posts whose current status is `ready`;
- `staging_sent` — sent runs with only a staging receipt;
- `public_sent` — sent runs with a positive production `Post.telegram_message_id`.

The report also calculates descriptive conversion rates from `found`, plus
`public_sent_from_ready`. Sources with zero items remain visible so a persistently silent/dead source
is not hidden by the report.

## Attribution

Version 1 deliberately uses `anchor_source` attribution. The source of the `RawItem` referenced by
`EditorialRun.raw_item_id` owns that funnel row. Other official sources that appeared in the stored
evidence bundle are not credited with another publication.

This prevents one multi-source event from inflating yield across every confirming source. A future
separate evidence-contribution report may answer a different question: which sources were useful as
corroboration.

## Window

The requested interval is half-open `[since, until)` and is based on `RawItem.ingested_at`, not the
publisher's article date. Both timestamps must contain an explicit timezone offset and are normalized
to UTC in the output.

Example:

```bash
relocant-source-yield \
  --city belgrade \
  --since 2026-10-14T00:00:00+02:00 \
  --until 2026-10-21T00:00:00+02:00
```

The command reads the configured newsroom database and prints JSON. It performs no collection,
editorial processing, Telegram delivery, source mutation or LLM call.

## Interpretation

Low `reviewed_from_found` can mean a source produces a lot of irrelevant/duplicate material. Low
`post_from_found` can indicate weak audience value or evidence quality. High `ready` but low
`public_sent` should be investigated as an operational/delivery issue rather than immediately blamed
on the source.

The report is descriptive. It must not automatically disable a source, weaken evidence requirements,
or change editorial ranking. In particular, new shadow snapshot sources are expected to have low or
zero publication yield until a structured parser is intentionally enabled.
