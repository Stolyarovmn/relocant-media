# Telegram staging bootstrap

This runbook creates the staging Telegram topology from the owner's already-authorized MTProto session.

The bootstrap is intentionally separate from the analytics adapter and scheduler. Runtime analytics remain read-only. The bootstrap command is an explicit owner action because it creates Telegram resources.

Official Telegram API behavior used here:

- `channels.createChannel` creates channels/supergroups and supports the `forum` flag;
- `channels.togglePreHistoryHidden(enabled=false)` makes old discussion-group messages visible to new members, which Telegram requires before linking a supergroup as a channel discussion group;
- `channels.setDiscussionGroup` links a supergroup to a broadcast channel;
- Telegram does **not** allow forum mode on a supergroup that is linked as a channel discussion group, so the forum is a separate supergroup.

## Resulting topology

```text
Relocant Media — Belgrade [STAGING]
└── linked discussion: Relocant Media — Belgrade Comments [STAGING]

Relocant Media — Belgrade Forum [STAGING]
└── separate forum supergroup for staging workflows/testing
```

The bootstrap does not create a Telegram bot and does not automate BotFather. Bot creation and token retrieval remain an explicit owner step.

## Prerequisites

1. `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are present in the host `.env`.
2. The personal-account session has already been authorized:

```bash
docker compose --profile analytics run --rm analytics relocant-mtproto login
```

3. `APP_ENV` is not `prod`.

## Preview without side effects

```bash
docker compose --profile analytics run --rm analytics \
  relocant-telegram-bootstrap --city Belgrade plan
```

The plan prints the titles and exact resource-creation steps. It performs no Telegram API writes.

## Create the staging topology

```bash
docker compose --profile analytics run --rm analytics \
  relocant-telegram-bootstrap --city Belgrade apply --confirm-create-staging
```

The command creates or resumes:

1. broadcast staging channel;
2. discussion supergroup;
3. visible discussion history;
4. channel ↔ discussion-group link;
5. separate forum supergroup.

The command writes a private journal to:

```text
/run/telegram/staging-topology.json
```

inside the same private Docker volume as the MTProto session. File mode is `0600`.

The journal contains only topology IDs/titles/state. It does not contain the phone number, login code, 2FA password, `api_hash`, Bot API token, or MTProto session bytes.

## Crash/retry behavior

The bootstrap is resumable. State is persisted after each confirmed Telegram action. If the command fails halfway through, run the same `apply` command again. It continues from the last stored step instead of creating another channel/group pair.

If the topology reaches `status=ready`, repeated `apply` calls perform no creation operations.

Do not delete the journal merely to retry a failed operation: deleting it removes the duplicate-protection mechanism.

## Verify the topology

```bash
docker compose --profile analytics run --rm analytics \
  relocant-telegram-bootstrap --city Belgrade check
```

The check verifies:

- the channel is a broadcast channel;
- the discussion resource is a supergroup;
- Telegram reports a linked discussion group for the channel;
- the forum resource is both a supergroup and a forum.

It also prints environment mappings similar to:

```json
{
  "TELEGRAM_STAGING_CHANNEL_ID": "-100...",
  "TELEGRAM_STAGING_DISCUSSION_GROUP_ID": "-100...",
  "TELEGRAM_STAGING_FORUM_GROUP_ID": "-100...",
  "TELEGRAM_MTPROTO_CHANNEL_ID": "-100..."
}
```

Copy these values into host secret/environment management. The command never edits `.env` automatically.

## BotFather owner step

After the topology exists:

1. open `@BotFather` in an official Telegram client;
2. create the staging bot with `/newbot`;
3. store the returned token as `TELEGRAM_BOT_TOKEN` in host secret management;
4. add the bot to the staging channel/discussion/forum with only the permissions required by the application;
5. run the normal Telegram smoke checks before setting `TELEGRAM_RUNTIME_ENABLED=true`.

Never paste the bot token into an issue, PR, commit, CI log, or chat transcript.

## Cleanup / rollback

The bootstrap intentionally does not auto-delete Telegram resources because destructive cleanup should be visually confirmed in Telegram.

If a test topology must be discarded:

1. delete the staging channel/groups from an official Telegram client;
2. confirm they are gone;
3. remove `/run/telegram/staging-topology.json` from the private analytics volume;
4. rerun `plan`, then `apply` if a fresh topology is needed.

Deleting the journal without deleting the Telegram resources can create duplicates on the next `apply`.
