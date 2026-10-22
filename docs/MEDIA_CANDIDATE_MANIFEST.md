# Saved-page media candidate manifest

`relocant-media-candidates` turns an **already saved** UTF-8 HTML snapshot of a verified
source page into a provenance manifest for visual review. The command performs no network
requests, does not call AI or Telegram, does not download image bytes and never grants
reuse rights.

```sh
APP_ENV=staging relocant-media-candidates \
  --source-page https://official.example/article/42 \
  --html-file /run/evidence/article.html \
  --image-host media.official.example \
  --output /new/run/media-candidates
```

`--image-host` may be repeated only for CDN hosts whose relation to the source page has
already been verified. Without it, only images on the source-page host survive extraction.
The command keeps a byte-identical `source.html`, stores its SHA256 and size, then writes
`manifest.json` with normalized candidates and `event_match=unreviewed`.

The manifest is deliberately pre-review: `publication_permitted=false`,
`generated_media_used=false`, and candidates do not contain `rights_verified`. A non-empty
candidate list requires the existing independent media reviews before an asset can enter
publication. If no candidate survives, text-only publication remains the preferred result.

The HTML snapshot is capped at 5 MiB and the output directory must be new. The staging
guard is checked before reading the snapshot. This tool is therefore safe to call after an
existing collector/evidence step without creating a second arbitrary-URL fetch path.

## Automatic ingestion sidecar

Collectors that already fetched a full authoritative/source page may attach that exact HTML
to their internal `RawItem`. Ingestion then runs the same generic candidate extraction
locally and persists a `raw_item_media_snapshot` sidecar keyed by `raw_item_id`. No second
HTTP request is made for media discovery.

The sidecar records the source page, exact saved HTML when it is at most 5 MiB, SHA256,
byte count, approved image hosts and the candidate manifest. Oversized snapshots are not
stored or parsed for candidates; the manifest explicitly falls back to `mode=text`.
Extraction failures are also fail-closed to text-only publication and do not block otherwise
valid factual ingestion.

Once any `EditorialRun` references a raw item, its media sidecar is frozen. A later fetch of
the same factual item cannot replace the HTML or candidate list behind an already reviewed
editorial context. Factual source changes continue through the existing source-revision and
human-review path.

The sidecar is **not** a media release. `publication_permitted` remains false,
`event_match` remains unreviewed, no rights are inferred from source ownership or credits,
and the existing rights verification plus two independent media reviews are still mandatory
before local image bytes can be delivered. A missing/rejected/oversized sidecar simply means
text-only publication.

## READY media review queue

`relocant-media-review` materializes review input from the existing database; it does not
perform another source request or image download. A queue item exists only when the
`EditorialRun` is `READY`, has a persisted reviewed package hash, and at least one referenced
raw item has an intact immutable media sidecar.

```sh
APP_ENV=staging \
DATABASE_URL=sqlite:////srv/relocant/state/newsroom.sqlite3 \
relocant-media-review --output /new/run/media-review
```

Without `--raw-item-id`, the command exports the newest pending review item. It writes
`packet.json` plus `assessment-template.json`. Every template deliberately starts with
`kind=unknown`, `event_match=false`, `visual_value=0`, `rights_method=unknown`,
`rights_status=needs_review` and an empty `assessed_by`; it therefore cannot be accepted as a
`CoverAssessment` until an explicit reviewer fills the missing judgement and rights evidence.
Attribution discovered on the source page may be copied as provenance context, but it does
not grant reuse rights.

A completed assessment file can be checked against the exact ingested candidate set and the
existing deterministic `plan_cover` policy:

```sh
APP_ENV=staging \
DATABASE_URL=sqlite:////srv/relocant/state/newsroom.sqlite3 \
relocant-media-review \
  --raw-item-id 123 \
  --assessments /review/assessments.json \
  --event-year 2026 \
  --output /new/run/media-review-123
```

The resulting `cover-plan.json` is still pre-download. The command always reports
`download_permitted=false` and `publication_permitted=false`; it never creates a `MediaAsset`
and never sets `rights_verified`. Selected local bytes must still be obtained through an
approved downloader and then pass the existing hash-bound `MediaRelease` validation with two
independent `facts`/`utility` reviews before staging delivery. Invalid or tampered source HTML
fails closed to text-only mode.
