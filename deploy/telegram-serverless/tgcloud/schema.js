import { index, integer, json, table, text } from "sdk/db";

export const inbox = table("intake_inbox", {
  sequence: integer("sequence").primaryKey({ autoIncrement: true }),
  updateId: integer("update_id").notNull().unique(),
  actor: text("actor"),
  event: json("event").notNull(),
  draft: json("draft"),
  notice: json("notice"),
  delivery: text("delivery").notNull(),
  attemptedAt: integer("attempted_at"),
  acked: integer("acked").notNull().default(0),
}, (row) => ({
  actorOrder: index("intake_inbox_actor_sequence").on(row.actor, row.sequence),
  pendingOrder: index("intake_inbox_acked_sequence").on(row.acked, row.sequence),
}));

export const seed = table("intake_seed", {
  actor: text("actor").primaryKey(),
  draft: json("draft").notNull(),
});

export const settings = table("intake_settings", {
  id: integer("id").primaryKey(),
  value: json("value").notNull(),
});
