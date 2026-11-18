import { createHash, randomBytes, randomUUID } from "node:crypto";
import { Conflict, paths } from "./store.js";
import { HttpError, equalSecret } from "./http.js";
import { ReplyBot } from "./bot.js";
import { planDraft, readerIdentity } from "./intake.js";

export const LIMITS = Object.freeze({
  updateBytes: 512 * 1024, bootstrapBytes: 4 * 1024 * 1024,
  exportBytes: 480 * 1024,
  queueBytes: 4 * 1024 * 1024, stateBytes: 8 * 1024 * 1024,
  receiptCount: 30_000, receiptMs: 25 * 60 * 60 * 1000,
  // Must exceed vercel.json maxDuration (30 seconds), including runtime teardown.
  attemptMs: 45_000, reserveBytes: 128 * 1024,
});
const terminal = new Set(["new", "cancelled"]);
const sha = (text) => createHash("sha256").update(text).digest("hex");
const bytes = (value) => Buffer.byteLength(JSON.stringify(value));
const clone = (value) => structuredClone(value);
const validId = (value) => Number.isSafeInteger(value) && value > 0;

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(",")}]`;
  if (value && typeof value === "object") return `{${Object.keys(value).sort()
    .map((key) => `${JSON.stringify(key)}:${stable(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

function nextReference(cases) {
  const alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  for (let attempt = 0; attempt < 32; attempt += 1) {
    const token = [...randomBytes(8)].map((n) => alphabet[n & 31]).join("");
    const reference = `RM-${token.slice(0, 4)}-${token.slice(4)}`;
    if (!cases.some((item) => item.reference === reference)) return reference;
  }
  throw new HttpError(503, "reference_exhausted");
}

function receivedAt(message, now) {
  const date = message?.date;
  return new Date(Number.isSafeInteger(date) && date > 0 && date * 1000 <= now + 60_000
    ? Math.min(now, date * 1000) : now).toISOString();
}

function actorMatches(item, actor) {
  return item.telegram_user_id === actor.user && item.payload.chat_id === actor.chat &&
    (item.payload.topic_id ?? null) === actor.topic;
}

function actorKey(actor) { return `${actor.user}:${actor.chat}:${actor.topic ?? ""}`; }

function caseActor(item) {
  return { user: item.telegram_user_id, chat: item.payload.chat_id, topic: item.payload.topic_id ?? null };
}

function callbackActor(update) {
  const callback = update.callback_query;
  if (typeof callback?.data !== "string" || !callback.data.startsWith("intake:") ||
      callback.from?.is_bot || !validId(callback.from?.id) ||
      !Number.isSafeInteger(callback.message?.chat?.id)) return null;
  const topic = callback.message.direct_messages_topic?.topic_id ?? null;
  if (topic !== null && !validId(topic)) return null;
  return { user: callback.from.id, chat: callback.message.chat.id, topic };
}

function locateCase(state, actor, message, edited) {
  const cases = state.cases.filter((item) => actorMatches(item, actor)).reverse();
  if (edited) return cases.find((item) =>
    Object.hasOwn(item.payload.reader_parts ?? {}, message.message_id)) ?? null;
  const target = message.reply_to_message?.message_id;
  const related = target && cases.find((item) => [
    ...Object.values(item.payload.reader_notices ?? {}),
    ...Object.values(item.payload.feedback ?? {}),
  ].some((notice) => notice.status === "sent" && notice.telegram_message_id === target));
  return related || cases.find((item) => ["choosing", "collecting"].includes(item.status)) || cases[0] || null;
}

function cleanNotices(draft) {
  draft.payload.reader_notice_pending = false;
  for (const [key, notice] of Object.entries(draft.payload.reader_notices ?? {})) {
    if (notice.status === "pending") draft.payload.reader_notices[key] = {
      ...notice, status: "superseded",
    };
    // A seeded interrupted send cannot be retried by this writer either.
    if (notice.status === "sending") draft.payload.reader_notices[key] = {
      ...notice, status: "uncertain",
    };
  }
}

function checkCapacity(state, reserve = 0) {
  if (bytes(state.events) + (state.busy ? bytes(state.busy.event) : 0) + reserve > LIMITS.queueBytes ||
      bytes(state) + reserve > LIMITS.stateBytes ||
      Object.keys(state.receipts).length > LIMITS.receiptCount) {
    throw new HttpError(503, "queue_capacity");
  }
}

function pruneReceipts(state, now) {
  for (const [id, receipt] of Object.entries(state.receipts)) {
    if (receipt.at < now - LIMITS.receiptMs && receipt.sequence <= state.ack_sequence) {
      delete state.receipts[id];
    }
  }
}

function finish(state, busy, notice) {
  const event = clone(busy.event);
  if (event.draft) {
    if (notice) {
      event.notice = notice;
      event.draft.payload.reader_notices ??= {};
      event.draft.payload.reader_notices[notice.key] = notice;
    }
    event.draft.payload.reader_notice_pending = false;
    const index = state.cases.findIndex((item) => item.reference === event.draft.reference);
    if (index === -1) state.cases.push(clone(event.draft));
    else state.cases[index] = clone(event.draft);
    state.case_sequences[event.draft.reference] = event.sequence;
    // Pending seed prompts are retained internally for early keyboard input
    // and coalescing; Actions owns no send attempts for these historical prompts.
    cleanNotices(event.draft);
  }
  state.events.push(event);
  state.busy = null;
  return event;
}

function validateSeed(seed) {
  if (seed?.version !== 1 || !validId(seed.bot_id) || !Number.isSafeInteger(seed.channel_id) ||
      seed.channel_id >= 0 || !seed.form || typeof seed.form.new_case !== "string" ||
      typeof seed.form.menu !== "string" || !seed.form.routes ||
      !Array.isArray(seed.cases) || !Array.isArray(seed.processed_updates) ||
      !Array.isArray(seed.legacy_conversations) ||
      !seed.legacy_conversations.every(validId) ||
      !seed.processed_updates.every((id) => Number.isSafeInteger(id) && id >= 0)) {
    throw new HttpError(400, "invalid_seed");
  }
  const kinds = ["news_tip", "complaint", "correction", "partnership", "ad_request", "technical", "question"];
  if (Object.keys(seed.form.routes).length !== kinds.length || !kinds.every((key) =>
    Array.isArray(seed.form.routes[key]) && seed.form.routes[key].length === 2 &&
    seed.form.routes[key].every((value) => typeof value === "string" && value.length > 0))) {
    throw new HttpError(400, "invalid_form");
  }
  const references = new Set();
  for (const item of seed.cases) {
    if (!validId(item.id) || !validId(item.telegram_user_id) || !validId(item.telegram_message_id) ||
        typeof item.reference !== "string" || references.has(item.reference) ||
        !["choosing", "collecting", "new", "waiting_primary", "source_linked"].includes(item.status) ||
        ![...kinds, "reader_unclassified"].includes(item.kind) ||
        item.payload?.reader_intake !== true || !Number.isSafeInteger(item.payload.chat_id) ||
        (item.payload.public_reference !== undefined && item.payload.public_reference !== item.reference)) {
      throw new HttpError(400, "invalid_seed_case");
    }
    references.add(item.reference);
    item.payload.public_reference = item.reference;
  }
}

export class Ingress {
  constructor({ store, env = process.env, now = Date.now, botFactory = (config, enabled) =>
    new ReplyBot(config, enabled), hooks = {} }) {
    this.store = store;
    this.env = env;
    this.now = now;
    this.botFactory = botFactory;
    this.hooks = hooks;
    this.paths = paths(env);
  }

  async config() {
    const result = await this.store.read(this.paths.config);
    if (!result) throw new HttpError(503, "not_bootstrapped");
    return result.value;
  }

  canSend(state) { return this.env.VERCEL_ENV === "production" && state.mode === "active"; }

  async transaction(change) {
    for (let attempt = 0; attempt < 8; attempt += 1) {
      const current = await this.store.read(this.paths.state);
      if (!current) throw new HttpError(503, "not_bootstrapped");
      const state = clone(current.value);
      const result = change(state);
      if (result?.unchanged) return result.value;
      checkCapacity(state);
      try {
        await this.store.write(this.paths.state, state, current.etag);
        return result;
      } catch (error) { if (!(error instanceof Conflict)) throw error; }
    }
    throw new HttpError(503, "concurrent_update");
  }

  async bootstrap(input) {
    if (typeof input.seed_json !== "string" || !/^[a-f0-9]{64}$/u.test(input.seed_sha256 ?? "") ||
        sha(input.seed_json) !== input.seed_sha256 || !["active", "preview"].includes(input.mode) ||
        typeof input.bot_token !== "string" || !/^\d{5,}:[A-Za-z0-9_-]{20,}$/u.test(input.bot_token) ||
        typeof input.webhook_secret !== "string" || !/^[A-Za-z0-9_-]{32,256}$/u.test(input.webhook_secret)) {
      throw new HttpError(400, "invalid_bootstrap");
    }
    let seed;
    try { seed = JSON.parse(input.seed_json); } catch { throw new HttpError(400, "invalid_seed"); }
    validateSeed(seed);
    if (Number(input.bot_token.split(":")[0]) !== seed.bot_id) throw new HttpError(400, "bot_identity");
    const config = {
      version: 1, seed_sha256: input.seed_sha256, bot_id: seed.bot_id,
      bot_token: input.bot_token, webhook_secret: input.webhook_secret,
    };
    let existing = await this.store.read(this.paths.config);
    if (!existing) {
      try { await this.store.write(this.paths.config, config); }
      catch (error) { if (!(error instanceof Conflict)) throw error; }
      existing = await this.store.read(this.paths.config);
    }
    if (!existing || existing.value.seed_sha256 !== config.seed_sha256 ||
        !equalSecret(existing.value.bot_token, config.bot_token) ||
        !equalSecret(existing.value.webhook_secret, config.webhook_secret)) {
      throw new HttpError(409, "bootstrap_conflict");
    }
    const now = this.now();
    const activeCases = seed.cases.filter((item) => ["choosing", "collecting"].includes(item.status));
    const activeActors = new Set(activeCases.map((item) => actorKey(caseActor(item))));
    const state = {
      version: 1, seed_sha256: input.seed_sha256, mode: input.mode,
      bot_id: seed.bot_id, channel_id: seed.channel_id, form: seed.form,
      cases: activeCases,
      // Non-draft seed cases are already in the canonical checkpoint. Keep no
      // copied content here; future messages for these actors go to Actions.
      relay_actors: Object.fromEntries(seed.cases.filter((item) =>
        !["choosing", "collecting"].includes(item.status) && !activeActors.has(actorKey(caseActor(item))))
        .map((item) => [actorKey(caseActor(item)), 0])),
      legacy_conversations: seed.legacy_conversations,
      receipts: Object.fromEntries(seed.processed_updates.map((id) => [id, { at: now, hash: null, sequence: 0 }])),
      case_sequences: {}, events: [], next_sequence: 1, ack_sequence: 0,
      exported_through: 0, checkpoint_sha256: null, busy: null,
    };
    checkCapacity(state, LIMITS.reserveBytes);
    const previous = await this.store.read(this.paths.state);
    if (!previous) {
      try { await this.store.write(this.paths.state, state); }
      catch (error) { if (!(error instanceof Conflict)) throw error; }
    }
    const actual = (await this.store.read(this.paths.state))?.value;
    if (!actual || actual.seed_sha256 !== input.seed_sha256 || actual.mode !== input.mode) {
      throw new HttpError(409, "bootstrap_conflict");
    }
    return this.status(actual);
  }

  status(state) {
    return {
      status: "ready", seed_sha256: state.seed_sha256, mode: state.mode,
      pending_count: state.events.length + (state.busy ? 1 : 0),
      open_draft_count: state.cases.filter((item) => ["choosing", "collecting"].includes(item.status)).length,
      busy: Boolean(state.busy), next_sequence: state.next_sequence,
      ack_sequence: state.ack_sequence, can_send: this.canSend(state),
    };
  }

  async recover(config) {
    const busy = await this.transaction((state) => {
      if (!state.busy || state.busy.expires_at > this.now()) return { unchanged: true, value: null };
      if (state.busy.phase === "sending") {
        finish(state, state.busy, { ...state.busy.notice, status: "uncertain", error_type: "InterruptedSend" });
        return null;
      }
      // The previous invocation is guaranteed to have terminated after the
      // configured 30s runtime plus 15s grace. Its prepared plan can be reclaimed.
      state.busy.id = randomUUID();
      state.busy.expires_at = this.now() + LIMITS.attemptMs;
      return clone(state.busy);
    });
    if (busy) await this.deliver(busy, config);
  }

  async deliver(busy, config) {
    const sending = await this.transaction((state) => {
      if (state.busy?.id !== busy.id || state.busy.phase !== "prepared" ||
          state.busy.expires_at <= this.now()) throw new HttpError(503, "attempt_lost");
      state.busy.phase = "sending";
      state.busy.notice.status = "sending";
      state.busy.notice.attempted_at = new Date(this.now()).toISOString();
      return { busy: clone(state.busy), enabled: this.canSend(state) };
    });
    await this.hooks.afterSending?.();
    let notice = { ...sending.busy.notice, status: "uncertain" };
    try {
      const result = await this.botFactory(config, sending.enabled).call("sendMessage", sending.busy.params);
      if (!validId(result?.message_id)) throw new Error("invalid_receipt");
      const sentAt = new Date(this.now()).toISOString();
      notice = {
        ...notice, status: "sent", telegram_message_id: result.message_id, sent_at: sentAt,
        response_seconds: Math.max(0, (this.now() - Date.parse(notice.received_at)) / 1000),
      };
    } catch { notice.error_type = "BotAPIUncertain"; }
    await this.hooks.afterSend?.();
    await this.transaction((state) => {
      if (state.busy?.id !== busy.id || state.busy.phase !== "sending") {
        throw new HttpError(503, "attempt_lost");
      }
      finish(state, state.busy, notice);
      return null;
    });
  }

  async receive(update, config) {
    if (!Number.isSafeInteger(update.update_id) || update.update_id < 0) throw new HttpError(400, "invalid_update");
    const hash = sha(stable(update));
    await this.recover(config);
    // Parent DM verification is a read-only BotAPI call. It precedes planning;
    // the final state/duplicate decision is repeated inside the CAS transaction.
    const current = (await this.store.read(this.paths.state))?.value;
    if (!current) throw new HttpError(503, "not_bootstrapped");
    const existing = current.receipts[update.update_id];
    if (existing) {
      if (existing.hash !== null && existing.hash !== hash) throw new HttpError(409, "update_conflict");
      if (current.busy?.event.update.update_id === update.update_id) throw new HttpError(503, "attempt_busy");
      return { status: "ready" };
    }
    const message = update.callback_query ? null : (update.message ?? update.edited_message);
    let actor = message && readerIdentity(message);
    if (actor && current.legacy_conversations.includes(actor.user)) actor = null;
    if (actor?.topic !== null && actor) {
      if (!this.canSend(current)) actor = null;
      else {
        const chat = await this.botFactory(config, true).call("getChat", { chat_id: actor.chat });
        if (chat?.is_direct_messages !== true || chat.parent_chat?.id !== current.channel_id) actor = null;
      }
    }
    if (actor) actor.channel = current.channel_id;
    const prepared = await this.transaction((state) => {
      pruneReceipts(state, this.now());
      const duplicate = state.receipts[update.update_id];
      if (duplicate) {
        if (duplicate.hash !== null && duplicate.hash !== hash) throw new HttpError(409, "update_conflict");
        if (state.busy?.event.update.update_id === update.update_id) throw new HttpError(503, "attempt_busy");
        return { unchanged: true, value: null };
      }
      if (state.busy) throw new HttpError(503, "attempt_busy");
      const event = { sequence: state.next_sequence++, update: clone(update) };
      const callback = callbackActor(update);
      if (callback) {
        const key = actorKey(callback);
        state.relay_actors[key] = event.sequence;
        // Canonical processing of this callback may change the draft. All later
        // updates for its actor must relay, so a stale local envelope cannot
        // overwrite that decision. Earlier envelopes remain ordered in events.
        state.cases = state.cases.filter((item) => !actorMatches(item, callback));
      }
      if (actor && Object.hasOwn(state.relay_actors, actorKey(actor))) {
        state.relay_actors[actorKey(actor)] = event.sequence;
      }
      const planned = actor && !Object.hasOwn(state.relay_actors, actorKey(actor)) &&
        planDraft(state.form, locateCase(state, actor, message, Boolean(update.edited_message)),
        message, Boolean(update.edited_message), () => nextReference(state.cases), actor, receivedAt(message, this.now()));
      state.receipts[update.update_id] = { at: this.now(), hash, sequence: event.sequence };
      if (planned) event.draft = clone(planned.draft);
      let notice = planned?.notice;
      if (notice && Object.hasOwn(event.draft.payload.reader_notices ?? {}, notice.key)) notice = null;
      const pending = notice && this.canSend(state);
      const busy = {
        id: randomUUID(), phase: "prepared", expires_at: this.now() + LIMITS.attemptMs, event,
        ...(pending && {
          notice: { ...notice, received_at: receivedAt(message, this.now()), status: "pending", reply_to: message.message_id },
          params: {
            chat_id: actor.chat, ...(actor.topic !== null && { direct_messages_topic_id: actor.topic }),
            text: notice.text, protect_content: true, allow_paid_broadcast: false,
            link_preview_options: { is_disabled: true },
            ...(notice.reply_markup && { reply_markup: notice.reply_markup }),
            reply_parameters: { message_id: message.message_id, allow_sending_without_reply: true },
          },
        }),
      };
      // Every accepted event must fit in one Actions response, including the
      // final delivery receipt. Reserve its duplicate in draft.reader_notices.
      const exported = clone(event);
      if (pending) {
        const receipt = { ...busy.notice, status: "uncertain", attempted_at: new Date(this.now()).toISOString(),
          sent_at: new Date(this.now()).toISOString(), telegram_message_id: Number.MAX_SAFE_INTEGER,
          response_seconds: Number.MAX_SAFE_INTEGER, error_type: "InterruptedSend" };
        exported.notice = receipt;
        exported.draft.payload.reader_notices ??= {};
        exported.draft.payload.reader_notices[receipt.key] = receipt;
      }
      if (bytes({ status: "ready", events: [exported] }) > LIMITS.exportBytes) {
        throw new HttpError(413, "event_too_large");
      }
      if (pending) state.busy = busy;
      else finish(state, busy, null);
      checkCapacity(state, LIMITS.reserveBytes);
      return pending ? clone(busy) : null;
    });
    await this.hooks.afterPrepared?.();
    if (prepared) await this.deliver(prepared, config);
    return { status: "ready" };
  }

  async transfer(input) {
    if (input.operation === "bootstrap") return this.bootstrap(input);
    if (input.operation === "status") {
      const state = await this.store.read(this.paths.state);
      if (!state) throw new HttpError(503, "not_bootstrapped");
      return this.status(state.value);
    }
    if (!["peek", "ack"].includes(input.operation)) throw new HttpError(400, "unknown_operation");
    if (input.operation === "peek") {
      const config = await this.config();
      await this.recover(config);
      const after = input.after ?? 0;
      const limit = input.limit ?? 30;
      if (!Number.isSafeInteger(after) || after < 0 || !Number.isInteger(limit) || limit < 1 || limit > 30) {
        throw new HttpError(400, "invalid_peek");
      }
      return this.transaction((state) => {
        const events = [];
        for (const event of state.events) {
          if (event.sequence <= Math.max(after, state.ack_sequence)) continue;
          if (events.length >= limit || bytes({ status: "ready", events: [...events, event] }) > LIMITS.exportBytes) break;
          events.push(event);
        }
        if (events.length) state.exported_through = Math.max(state.exported_through, events.at(-1).sequence);
        return { status: "ready", events: clone(events) };
      });
    }
    if (input.operation === "ack") {
      if (!Number.isSafeInteger(input.through) || input.through < 0 ||
          !/^[a-f0-9]{64}$/u.test(input.snapshot_sha256 ?? "")) throw new HttpError(400, "invalid_ack");
      let canonicalOpen = null;
      if (input.open_drafts !== undefined) {
        if (!Array.isArray(input.open_drafts) || input.open_drafts.some((item) =>
          !item || typeof item.reference !== "string" || item.reference.length > 64 ||
          !validId(item.telegram_user_id) || !Number.isSafeInteger(item.chat_id) ||
          (item.topic_id !== null && !validId(item.topic_id)))) throw new HttpError(400, "invalid_open_drafts");
        canonicalOpen = new Set(input.open_drafts.map((item) => actorKey({
          user: item.telegram_user_id, chat: item.chat_id, topic: item.topic_id,
        })));
      }
      return this.transaction((state) => {
        // The first checkpoint already proved this cursor. A later canonical
        // snapshot may have a different SHA without advancing intake. Replays
        // must not reapply cleanup or alter newer drafts, fences or attempts.
        if (input.through === state.ack_sequence && typeof state.checkpoint_sha256 === "string") {
          return { unchanged: true, value: this.status(state) };
        }
        if (input.through < state.ack_sequence || input.through > state.exported_through ||
            state.busy?.event.sequence <= input.through) throw new HttpError(409, "ack_conflict");
        state.events = state.events.filter((event) => event.sequence > input.through);
        state.cases = state.cases.filter((item) => !(terminal.has(item.status) &&
          (state.case_sequences[item.reference] ?? Infinity) <= input.through));
        const retained = new Set(state.cases.map((item) => item.reference));
        for (const reference of Object.keys(state.case_sequences)) {
          if (!retained.has(reference)) delete state.case_sequences[reference];
        }
        if (canonicalOpen) {
          for (const [key, sequence] of Object.entries(state.relay_actors)) {
            if (sequence <= input.through && !canonicalOpen.has(key)) delete state.relay_actors[key];
          }
        }
        state.ack_sequence = input.through;
        state.checkpoint_sha256 = input.snapshot_sha256;
        pruneReceipts(state, this.now());
        return this.status(state);
      });
    }
    throw new HttpError(400, "unknown_operation");
  }
}
