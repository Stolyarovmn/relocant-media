# Reviewed media asset materialization

This step bridges explicit cover assessment and the production media-release finalizer without granting reuse rights automatically.

## Safety model

`relocant-media-assets` runs only with `APP_ENV=staging` and an existing local SQLite newsroom snapshot. It never sends Telegram messages and never marks a post publishable.

The command accepts only `CoverAssessment` records that already passed the existing deterministic cover plan. `rights_verified=true` in the generated `MediaAsset` is a materialization of the explicit assessment: `rights_status=reviewed`, a non-unknown rights method, evidence URL, rationale, attribution and event match must already be present. URL/domain ownership alone never grants rights.

Before any download, the selected image must still be uniquely bound to the saved media review packet. The candidate is then re-extracted from the saved immutable source HTML. The downloader may use only the source host plus the exact `approved_image_hosts` saved during ingestion. Automatic redirects remain disabled; bounded same-allowlist redirects, size limits and PNG/JPEG signature checks are inherited from the existing article-media downloader.

For `current_photo`, automatic materialization currently requires `Source.ownership == "official"`, producing `origin=official_photo`. Non-official current photos fail closed instead of being mislabeled. `official_poster` remains `official_poster`; reviewed archive photos become `licensed_photo` and keep their required archive year/label.

The output contains:

- `assets/` — exact downloaded bytes, named by SHA-256;
- `assets.json` — the `MediaAsset` manifest consumed by `relocant-media-release prepare`;
- `materialization.json` — audit data binding source raw item, source HTML hash, saved image-host allowlist, reviewed image URL, final downloaded URL, assessor and rights evidence.

Both JSON files retain `publication_permitted=false` semantics. Independent facts/utility media reviews are still required later by `relocant-media-release finalize`.

## Workflow

After completing the explicit assessment created by `relocant-media-review`:

```bash
APP_ENV=staging \
DATABASE_URL=sqlite+pysqlite:///./newsroom.sqlite \
relocant-media-assets \
  --raw-item-id 123 \
  --assessments ./review/assessments.json \
  --event-year 2026 \
  --output ./review/materialized-123
```

If no assessment is eligible, the command returns text mode, performs no network request and does not create an output directory.

The exact bundle can then be transferred to the controlled production review host and frozen against the current editorial package:

```bash
APP_ENV=production \
DATABASE_URL="$DATABASE_URL" \
relocant-media-release prepare \
  --raw-item-id 123 \
  --release-id media-123 \
  --assets-manifest ./materialized-123/assets.json \
  --assets ./materialized-123/assets \
  --output ./review-bundles/123
```

`prepare` still creates only a non-publishable review context. Two explicit, independent `facts` and `utility` approvals are required before `relocant-media-release finalize` can create `release.json`. Production installation and Telegram delivery remain separate later steps.
