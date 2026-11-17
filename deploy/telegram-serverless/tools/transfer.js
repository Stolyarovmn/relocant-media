// Put this module under tgcloud/endpoints only in a temporary project used with
// tgcloud run. It is an operator tool and must never be deployed as a Mini App API.
import { db } from "sdk";
import { sql } from "sdk/db";

async function seed(state) {
  if (state.version !== 1 || !Array.isArray(state.cases) ||
      !Number.isSafeInteger(state.channel_id) || state.channel_id >= 0) {
    throw new Error("Invalid intake snapshot");
  }
  if (await db.get(sql`SELECT id FROM intake_settings WHERE id = 1`)) {
    throw new Error("Intake settings were already initialized");
  }
  const { cases, ...config } = state;
  for (const draft of cases) {
    const key = `${draft.telegram_user_id}:${draft.payload.chat_id}:${draft.payload.topic_id ?? ""}`;
    await db.run(sql`
      INSERT INTO intake_seed (actor, draft) VALUES (${key}, ${JSON.stringify(draft)})
      ON CONFLICT(actor) DO UPDATE SET draft = excluded.draft
    `);
  }
  await db.run(sql`INSERT INTO intake_settings (id, value) VALUES (1, ${JSON.stringify(config)})`);
  return { status: "configured", cases: cases.length };
}

async function peek(limit, after) {
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 100) {
    throw new Error("The limit must be between 1 and 100");
  }
  if (!Number.isSafeInteger(after) || after < 0) throw new Error("Invalid checkpoint");
  const abandoned = Date.now() - 120_000;
  await db.run(sql`
    UPDATE intake_inbox SET delivery = 'uncertain',
      notice = json_set(notice, '$.status', 'uncertain')
    WHERE sequence > ${after} AND delivery IN ('pending', 'sending')
      AND attempted_at < ${abandoned}
  `);
  const events = await db.all(sql`
    SELECT sequence, event, draft, notice, delivery FROM intake_inbox
    WHERE sequence > ${after} AND acked = 0 AND delivery IN ('ready', 'sent', 'uncertain')
    AND sequence < COALESCE((
      SELECT MIN(sequence) FROM intake_inbox
      WHERE sequence > ${after} AND acked = 0 AND delivery IN ('pending', 'sending')
    ), 9223372036854775807)
    ORDER BY sequence LIMIT ${limit}
  `);
  return {
    status: "ready",
    events: events.map((row) => ({
      sequence: row.sequence,
      update: JSON.parse(row.event),
      draft: row.draft && JSON.parse(row.draft),
      notice: row.notice && JSON.parse(row.notice),
    })),
  };
}

export default async function transfer(input, ctx) {
  if (ctx.initData || ctx.operator !== "relocant-intake") throw new Error("CLI only");
  if (input.operation === "seed") return JSON.stringify(await seed(input.state));
  if (input.operation === "peek") return JSON.stringify(await peek(input.limit, input.after ?? 0));
  if (input.operation === "ack") {
    if (!Number.isSafeInteger(input.sequence) || input.sequence < 1) {
      throw new Error("Valid checkpoint sequence is required");
    }
    await db.run(sql`
      UPDATE intake_inbox SET acked = 1 WHERE sequence <= ${input.sequence} AND acked = 0
    `);
    return JSON.stringify({ status: "acknowledged" });
  }
  throw new Error("Unsupported intake operation");
}
