# Telegram staging setup

The runtime in this repository is staging-only by default. `TELEGRAM_RUNTIME_ENABLED=false` means
the webhook only validates/classifies updates and performs no database or Telegram side effects.

## Owner-only setup

The Telegram account owner must create the bot and staging resources manually. Do not put bot tokens
or webhook secrets in Git, issue comments, CI logs, or source YAML.

Create separate staging resources and set only:

- `TELEGRAM_STAGING_CHANNEL_ID`
- `TELEGRAM_STAGING_DISCUSSION_GROUP_ID`
- optionally `TELEGRAM_STAGING_FORUM_GROUP_ID`

Keep production IDs in the separate `TELEGRAM_CHANNEL_ID`, `TELEGRAM_DISCUSSION_GROUP_ID`, and
`TELEGRAM_FORUM_GROUP_ID` variables. The staging publisher rejects a staging channel configured equal
to the production channel.

## Minimal bot permissions

For the staging broadcast channel, grant **Post messages** (`can_post_messages`). No permission to add
administrators is needed.

For the staging discussion supergroup, grant **Delete messages** (`can_delete_messages`) for the
current rule-based spam deletion path. Grant **Restrict members** (`can_restrict_members`) only if the
restriction action is enabled later. Telegram documents `restrictChatMember` as requiring the bot to
be an administrator with the appropriate rights.

`chat_member` updates about other users require the bot to be an administrator and must be explicitly
included in `allowed_updates`. The runtime uses an explicit allow-list rather than subscribing to every
new update type automatically.

## Webhook

Use an HTTPS webhook URL and a strong random `TELEGRAM_WEBHOOK_SECRET`. Configure Telegram's
`setWebhook` with that value as `secret_token`; Telegram sends it back in the
`X-Telegram-Bot-Api-Secret-Token` header. The `TelegramBotAPI.set_webhook()` helper also passes the
repository's explicit `allowed_updates` list.

Do not enable the runtime until Alembic migrations are applied and the staging chat IDs have been
verified by the owner.

## Reader intake

In a private chat with the bot:

- `/tip` starts a news-tip submission;
- `/correction` starts a correction submission;
- `/ad` starts an advertiser request;
- `/cancel` clears pending intake state.

A command can include the text on the same message (`/tip ...`) or the user can send the text as the
next message. Update IDs are persisted before processing, so Telegram retries do not duplicate a
submission or moderation action.
