# Temporary personal-account MTProto analytics

This is a launch-time exception. The analytics worker temporarily uses an additional Telegram session of the owner's existing personal account because a second phone number is not available yet.

The session is a **user credential**. Telegram executes MTProto calls with the identity and permissions of the authorized user, so treat the session file like a password.

Official references:

- https://core.telegram.org/api/auth
- https://core.telegram.org/method/channels.getMessages
- https://core.telegram.org/method/channels.getFullChannel
- https://core.telegram.org/api/stats

## Safety boundary

The implementation exposes only analytics reads:

- `channels.getMessages` for post counters;
- `channels.getFullChannel` for observed subscriber count.

It deliberately does not expose helpers for sending, editing or deleting messages, joining chats, inviting users, moderation, DMs or advertising actions.

`messages.getMessagesViews` is not used because that method can increment the view counter. The analytics adapter uses `channels.getMessages` instead.

The Bot API runtime and the personal MTProto session are isolated:

- `api` does not receive `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` or the session volume;
- `analytics` does not receive the Bot API token/webhook secret;
- the session lives in the named Docker volume `telegram_analytics_session` at `/run/telegram/analytics.session`;
- the session file is never committed, stored in PostgreSQL or printed by the setup CLI.

The separate `relocant-telegram-bootstrap` command is an explicit owner-only provisioning tool. It can create the staging Telegram topology, but it is not imported or invoked by the analytics scheduler; analytics remain read-only after provisioning.

## 1. Obtain Telegram API credentials

Log in to `https://my.telegram.org`, create an API application if needed and obtain:

- `api_id`;
- `api_hash`.

Put them only in the host `.env` file:

```dotenv
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=...
```

Do not put the phone number, login code or 2FA password in `.env`.

## 2. Build the analytics image

```bash
docker compose --profile analytics build analytics
```

## 3. Authorize the additional session

Run interactively from a trusted terminal:

```bash
docker compose --profile analytics run --rm analytics relocant-mtproto login
```

The CLI prompts for:

1. phone number;
2. Telegram login code;
3. 2FA password if the account has one.

All three prompts are interactive; none are command-line arguments or stored in the repository. After authorization the CLI prints only the Telegram user id/username and the internal session path.

Telegram may show a new-session notification in another client. Confirm that the login was yours.

## 4. Create the staging Telegram topology if it does not exist yet

Preview first:

```bash
docker compose --profile analytics run --rm analytics \
  relocant-telegram-bootstrap --city Belgrade plan
```

Then create/resume it:

```bash
docker compose --profile analytics run --rm analytics \
  relocant-telegram-bootstrap --city Belgrade apply --confirm-create-staging
```

The command prints the resulting `TELEGRAM_STAGING_*` and `TELEGRAM_MTPROTO_CHANNEL_ID` mappings. Copy those values into host environment/secret management. See `docs/TELEGRAM_STAGING_BOOTSTRAP.md` for the full runbook.

## 5. Verify read-only analytics access

After `TELEGRAM_MTPROTO_CHANNEL_ID` is configured:

```bash
docker compose --profile analytics run --rm analytics relocant-mtproto check
```

Expected output contains:

- `authorized: true`;
- your Telegram user id/username;
- current observed channel subscriber count;
- provenance `telegram_mtproto:channels.getFullChannel`.

No messages are sent during this check.

## 6. Run one scheduler tick with MTProto metrics

```bash
docker compose --profile analytics run --rm analytics \
  relocant-schedule tick \
  --city-config cities/belgrade/city.yaml \
  --sources cities/belgrade/sources
```

With a valid session the scheduler can now:

- store hourly `channel_snapshot` subscriber observations;
- populate due post snapshots at 1h/6h/24h/72h;
- continue normal source ingestion and daily reports.

If Telegram analytics is unavailable, the affected analytics task is marked `blocked`; missing values are not replaced with zero.

## Rotation / compromise

If the session may be compromised:

1. terminate the corresponding session in Telegram **Settings → Devices**;
2. delete/recreate the `telegram_analytics_session` Docker volume;
3. run `relocant-mtproto login` again.

Do not call a programmatic account logout as a rotation shortcut: revocation should be explicit and visible in Telegram's device/session controls.

## Long-term migration

When a separate technical Telegram account becomes available, authorize that account into the same isolated session volume. The scheduler/provider interface does not change; only the credentials/session are rotated.
