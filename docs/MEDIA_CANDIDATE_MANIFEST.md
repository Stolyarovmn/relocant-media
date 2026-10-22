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
