# Guarded production deployment

The production editorial checkout lives at `/opt/relocant-media/app`. A successful GitHub
merge does **not** by itself update that checkout, so presentation fixes in `main` are not
live until the server is deployed.

Use `deploy/server/deploy-green-main.sh` from the server as root. The script never starts an
editorial cycle and never calls Telegram. It only updates the runtime when all of these are
true:

- the checkout is on `main` and completely clean;
- the current checkout can fast-forward to the public GitHub `main`;
- the exact target SHA has a completed successful `ci` push run;
- the existing editorial worker lock can be acquired without killing a running cycle;
- private intake is not currently active;
- the editable virtualenv can be refreshed successfully;
- the persistent SQLite database passes the new code's exact schema/integrity checks;
- the deployed checkout still matches public `main` immediately before systemd units change.

During deployment only the two timers are stopped. A running editorial service is allowed to
finish naturally before the shared worker lock is obtained; a running private-intake service
causes deployment to defer instead of being killed. Timers are restored on every exit path.

If package installation or database validation fails, the checkout is reset to the previous
SHA and the previous editable package is restored best-effort. No migration is performed by
this deployment command: an incompatible SQLite schema blocks the rollout and requires the
existing explicit maintenance/backup path.

The script installs the four checked-in editorial/intake service and timer units and calls
`systemctl daemon-reload` only after code, dependencies, database contract, CI provenance and
main-SHA checks have succeeded.

For the current production channel this deployment is also the boundary that makes Telegram
presentation changes real. Current `main` hides the raw final source URL behind the clickable
`Источник` entity and asks Telegram for a large preview of the same verified primary-source
URL. Older server checkouts can therefore visibly produce long links/no preview even when the
repository has already fixed that behavior.
