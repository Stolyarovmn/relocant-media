# Staging deployment and owner setup

This runbook prepares the first deployable staging environment. It intentionally does not create or modify production Telegram resources.

## 1. Host prerequisites

Required on the host:

- Docker Engine with Compose v2;
- outbound HTTPS access to Telegram Bot API, Gemini API and configured sources;
- a public HTTPS endpoint that can forward `/telegram/webhook` to the API container before the Telegram runtime is enabled.

Clone the repository, then create the local secret file:

```bash
cp .env.example .env
chmod 600 .env
```

Generate a database password and webhook secret locally. Do not commit either value:

```bash
openssl rand -hex 24
openssl rand -hex 32
```

Update `POSTGRES_PASSWORD`, `DATABASE_URL` and `TELEGRAM_WEBHOOK_SECRET` in `.env`. If the database password contains URL-special characters, URL-encode it in `DATABASE_URL`.

## 2. Owner-only Telegram setup

These operations require the Telegram account that owns the staging resources.

1. Create a dedicated staging bot with BotFather and place its token in `TELEGRAM_BOT_TOKEN`.
2. Create a private staging channel. Add the bot as an administrator with the minimum rights needed to publish/edit staging posts and manage channel direct-message flows used by the project.
3. Create a separate supergroup linked to the staging channel as its discussion group. Give the bot the minimum moderation rights required to delete messages and restrict abusive members.
4. Create a separate forum supergroup for topical community discussions. Do not reuse the linked discussion group as the forum group.
5. Record the three numeric staging chat IDs in:
   - `TELEGRAM_STAGING_CHANNEL_ID`
   - `TELEGRAM_STAGING_DISCUSSION_GROUP_ID`
   - `TELEGRAM_STAGING_FORUM_GROUP_ID`
6. Set `TELEGRAM_WEBHOOK_URL` to the public HTTPS URL ending in `/telegram/webhook`.
7. Leave `TELEGRAM_RUNTIME_ENABLED=false` until the API, database and Telegram smoke checks are green.

Production channel/group IDs, if later configured, must be different from every staging ID. Readiness fails closed on collisions.

### Obtaining chat IDs without a third-party bot

Before a webhook is configured, add the bot to the staging resources and generate one harmless staging event in each resource. With `TELEGRAM_BOT_TOKEN` exported only in the current shell, inspect Bot API `getUpdates` and read the `chat.id` values. Do not paste the token into issues, logs or repository files.

If a webhook was already configured, remove/replace it through the owner-controlled Bot API workflow before using `getUpdates`.

## 3. Start the inert staging stack

With `TELEGRAM_RUNTIME_ENABLED=false`:

```bash
docker compose --profile staging up --build -d
```

The API image runs `alembic upgrade head` before starting Uvicorn. PostgreSQL must become healthy first.

Check liveness and readiness:

```bash
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/ready
```

Expected at this stage:

- `/health` returns HTTP 200 if the process is alive;
- `/ready` returns HTTP 200 when the database is reachable and the currently enabled feature set is configured;
- Telegram secrets are not required while `TELEGRAM_RUNTIME_ENABLED=false`.

Run the non-publishing smoke command inside the API container:

```bash
docker compose --profile staging exec api relocant-smoke
```

## 4. Configure and verify the Telegram webhook

After all staging IDs, token, secret and HTTPS webhook URL are present in `.env`, configure the webhook explicitly:

```bash
docker compose --profile staging exec api relocant-webhook
```

This calls Telegram `setWebhook` with the configured secret and the project's explicit `allowed_updates`. It does not publish a message.

Verify the bot, all staging chats and the currently registered webhook without publishing anything:

```bash
docker compose --profile staging exec api relocant-smoke --telegram
```

The smoke command uses only `getMe`, `getChat` and `getWebhookInfo` for Telegram checks.

## 5. Enable the staging runtime

Only after the previous smoke command succeeds:

1. set `TELEGRAM_RUNTIME_ENABLED=true` in `.env`;
2. restart the API container;
3. re-check `/ready`;
4. exercise intake/moderation only in staging resources.

```bash
docker compose --profile staging up -d --force-recreate api
curl -fsS http://127.0.0.1:8080/ready
docker compose --profile staging exec api relocant-smoke --telegram
```

When Telegram runtime is enabled, readiness returns HTTP 503 if any required Telegram setting is missing, the webhook is non-HTTPS, `APP_ENV=prod`, or a staging ID equals its production counterpart.

## 6. Operational commands

Run one ingestion cycle:

```bash
docker compose --profile staging exec api relocant-ingest --city belgrade --sources cities/belgrade/sources
```

Analytics/measurement commands are available through `relocant-measure`; use only observed metrics and existing database state.

Inspect logs:

```bash
docker compose --profile staging logs -f api postgres
```

Stop without deleting PostgreSQL data:

```bash
docker compose --profile staging down
```

Destroy the staging database volume only when intentionally resetting staging:

```bash
docker compose --profile staging down -v
```

## 7. Secret placement and rotation

Never commit `.env`, bot tokens, webhook secrets, Gemini keys, Anthropic keys, database passwords or future MTProto session strings.

Recommended placement:

- local/single-host staging: `.env` with mode `0600` outside backups that are not encrypted;
- GitHub Actions: repository/environment secrets;
- later managed runtime: the platform's native secret store.

Rotation procedure:

1. create the replacement secret/token;
2. update the runtime secret store;
3. for Telegram webhook secret, run `relocant-webhook` again;
4. restart the affected service;
5. run `relocant-smoke --telegram`;
6. revoke the previous credential only after the new path is verified.

Do not log secret values. Health/readiness endpoints return only missing-setting names and safe error class names.

## 8. Production boundary

Staging code must never target production channel/group IDs. Promotion to production requires a separate deployment decision, separate secret set and explicit owner approval. Paid-ad budget changes remain outside scheduler/runtime authority.
