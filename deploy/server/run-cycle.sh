#!/bin/sh
set -eu
output="/var/lib/relocant-media/runs/$(date -u +%Y%m%dT%H%M%S.%NZ)"
exec /usr/bin/flock -n /var/lib/relocant-media/worker.lock \
  /opt/relocant-media/venv/bin/python -m relocant_media.ops.server_cycle --output "$output"
