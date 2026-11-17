import { api, db } from "sdk";
import { sql } from "sdk/db";
import { planDraft, readerIdentity } from "./intake.js";

const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";

function nextReference() {
  let token = "";
  for (let i = 0; i < 8; i += 1) token += alphabet[Math.floor(Math.random() * alphabet.length)];
  return `RM-${token.slice(0, 4)}-${token.slice(4)}`;
}

export function actorKey(actor) {
  return `${actor.user}:${actor.chat}:${actor.topic ?? ""}`;
}

async function locateDraft(event, actor) {
  const draft = event.draft ? JSON.parse(event.draft) : null;
  if (!draft) return null;
  const rows = await db.all(sql`
    SELECT notice FROM intake_inbox
    WHERE actor = ${actor} AND notice IS NOT NULL
      AND json_extract(draft, '$.reference') = ${draft.reference}
    ORDER BY sequence
  `);
  draft.payload.reader_notices ??= {};
  for (const row of rows) {
    const notice = JSON.parse(row.notice);
    draft.payload.reader_notices[notice.key] = notice;
  }
  return draft;
}

async function sendNotice(updateId, actor) {
  const now = Date.now();
  const claimed = await db.run(sql`
    UPDATE intake_inbox SET delivery = 'sending', attempted_at = ${now}
    WHERE update_id = ${updateId} AND delivery = 'pending'
    RETURNING notice
  `);
  if (!claimed.rowsAffected) return;
  const notice = JSON.parse(claimed.rows[0].notice);
  let status = "uncertain";
  try {
    const response = await api.sendMessage({
      chat_id: actor.chat,
      ...(actor.topic !== null && { direct_messages_topic_id: actor.topic }),
      text: notice.text,
      protect_content: true,
      allow_paid_broadcast: false,
      ...(notice.reply_markup && { reply_markup: notice.reply_markup }),
      ...(notice.reply_to && { reply_parameters: {
        message_id: notice.reply_to, allow_sending_without_reply: true,
      } }),
    });
    if (Number.isSafeInteger(response.message_id)) {
      status = "sent";
      notice.telegram_message_id = response.message_id;
      notice.sent_at = new Date().toISOString();
      notice.response_seconds = Math.max(0, (Date.now() - Date.parse(notice.received_at)) / 1_000);
    }
  } finally {
    notice.status = status;
    await db.run(sql`
      UPDATE intake_inbox SET delivery = ${status}, notice = ${JSON.stringify(notice)}
      WHERE update_id = ${updateId} AND delivery = 'sending'
    `);
  }
}

async function actorFor(message, config) {
  if (!message || !config) return null;
  const actor = readerIdentity(message);
  if (!actor || config.legacy_conversations.includes(actor.user)) return null;
  if (actor.topic !== null) {
    const chat = await api.getChat({ chat_id: actor.chat });
    if (chat.is_direct_messages !== true || chat.parent_chat?.id !== config.channel_id) return null;
  }
  return { ...actor, channel: config.channel_id };
}

async function savePlain(update) {
  await db.run(sql`
    INSERT INTO intake_inbox (update_id, event, delivery)
    VALUES (${update.update_id}, ${JSON.stringify(update)}, 'ready')
    ON CONFLICT(update_id) DO NOTHING
  `);
}

export async function receive(update) {
  if (!Number.isSafeInteger(update?.update_id)) throw new Error("Telegram update ID is required");
  const setting = await db.get(sql`SELECT value FROM intake_settings WHERE id = 1`);
  const config = setting && JSON.parse(setting.value);
  if (config?.processed_updates.includes(update.update_id)) return;
  const message = update.message ?? update.edited_message;
  const actor = await actorFor(message, config);
  if (!actor) return savePlain(update);
  const key = actorKey(actor);
  const eventData = JSON.stringify(update);
  const receivedAt = new Date(Math.min(
    Date.now(), Number.isInteger(message.date) ? message.date * 1_000 : Date.now(),
  )).toISOString();
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const duplicate = await db.get(sql`
      SELECT delivery FROM intake_inbox WHERE update_id = ${update.update_id}
    `);
    if (duplicate) {
      if (duplicate.delivery === "pending") await sendNotice(update.update_id, actor);
      return;
    }
    const previous = await db.get(sql`
      SELECT sequence, draft, notice FROM intake_inbox
      WHERE actor = ${key} ORDER BY sequence DESC LIMIT 1
    `);
    const initial = !previous && await db.get(sql`
      SELECT draft FROM intake_seed WHERE actor = ${key}
    `);
    const existing = previous ?? initial;
    const planned = planDraft(
      config.form, existing ? await locateDraft(existing, key) : null, message,
      Boolean(update.edited_message), nextReference, actor, receivedAt,
    );
    if (!planned) return savePlain(update);
    const notice = planned.notice && {
      ...planned.notice,
      received_at: receivedAt,
      status: "pending",
      reply_to: message.message_id,
    };
    const inserted = await db.run(sql`
      INSERT INTO intake_inbox (update_id, actor, event, draft, notice, delivery, attempted_at)
      SELECT ${update.update_id}, ${key}, ${eventData}, ${JSON.stringify(planned.draft)},
             ${notice ? JSON.stringify(notice) : null}, ${notice ? "pending" : "ready"},
             ${notice ? Date.now() : null}
      WHERE COALESCE(
        (SELECT MAX(sequence) FROM intake_inbox WHERE actor = ${key}), 0
      ) = ${previous?.sequence ?? 0}
      ON CONFLICT(update_id) DO NOTHING
    `);
    if (!inserted.rowsAffected) continue;
    if (notice) await sendNotice(update.update_id, actor);
    return;
  }
  throw new Error("Concurrent changes to a reader draft");
}
