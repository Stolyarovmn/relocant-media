import { createHash } from "node:crypto";
import { Conflict } from "../lib/store.js";
import { Ingress } from "../lib/engine.js";

export class FakeStore {
  records = new Map();
  revision = 0;
  conflicts = 0;
  failWrite = null;
  async read(path) { return structuredClone(this.records.get(path) ?? null); }
  async write(path, value, etag = null) {
    if (this.failWrite?.(path, value)) throw new Error("simulated_storage_failure");
    if (this.conflicts > 0 && etag !== null) { this.conflicts--; throw new Conflict(); }
    const before = this.records.get(path);
    if (etag === null ? before : before?.etag !== etag) throw new Conflict();
    const next = String(++this.revision);
    this.records.set(path, { value: structuredClone(value), etag: next });
    return next;
  }
}

export function seed(extra = {}) {
  return {
    version: 1, bot_id: 123456, channel_id: -1001234,
    form: {
      new_case: "Новое обращение", menu: "Напишите сообщение и выберите тему.", routes: {
        news_tip: ["Предложить новость", "Что произошло?"],
        complaint: ["Пожаловаться", "На что жалоба?"],
        correction: ["Сообщить об ошибке", "Где ошибка?"],
        partnership: ["Редакционное партнёрство", "Ваш проект?"],
        ad_request: ["Платное размещение", "Ссылка и даты?"],
        technical: ["Карта или бот", "Какая проблема?"],
        question: ["Вопрос или идея", "Ваш вопрос?"],
      },
    },
    cases: [], processed_updates: [], legacy_conversations: [], ...extra,
  };
}

export function bootstrap(settings = seed(), mode = "active") {
  const seed_json = JSON.stringify(settings);
  return {
    operation: "bootstrap", seed_json,
    seed_sha256: createHash("sha256").update(seed_json).digest("hex"),
    bot_token: "123456:FAKE_TOKEN_ONLY_FOR_LOCAL_TESTING_123456",
    webhook_secret: "FAKE_WEBHOOK_SECRET_ONLY_FOR_LOCAL_TESTS", mode,
  };
}

export function message(id, text = "Вопрос", extra = {}) {
  return { update_id: id, message: {
    message_id: id + 100, date: 1_800_000_000,
    from: { id: 42, is_bot: false }, chat: { id: 42, type: "private" }, text, ...extra,
  } };
}

export async function fixture(options = {}) {
  const store = options.store ?? new FakeStore();
  let time = 1_800_000_001_000;
  const env = options.env ?? { VERCEL_ENV: "production", INGRESS_BRIDGE_TOKEN: "bridge-fixture" };
  const calls = [];
  const botFactory = options.botFactory ?? ((config, enabled) => ({ call: async (method, params) => {
    if (!enabled) throw new Error("preview_network_prohibited");
    calls.push({ method, params: structuredClone(params) });
    if (method === "getChat") return { is_direct_messages: true, parent_chat: { id: -1001234 } };
    const state = (await store.read("production/state.json"))?.value;
    if (state && state.busy?.phase !== "sending") throw new Error("send_without_durable_claim");
    if (method === "answerCallbackQuery") return true;
    return { message_id: 9000 + calls.length };
  } }));
  const deps = { store, env, now: () => time, botFactory, hooks: options.hooks ?? {} };
  const ingress = new Ingress(deps);
  const input = bootstrap(options.seed ?? seed(), options.mode ?? "active");
  await ingress.bootstrap(input);
  const config = await ingress.config();
  return {
    ingress, store, env, calls, deps, input, config,
    advance: (ms) => { time += ms; },
    receive: (update) => ingress.receive(update, config),
    state: async () => (await store.read(ingress.paths.state)).value,
  };
}

export async function invoke(handler, body, headers = {}, method = "POST") {
  const response = {
    headers: {}, statusCode: 0, data: null,
    setHeader(key, value) { this.headers[key] = value; },
    end(value) { this.data = JSON.parse(value); },
  };
  await handler({ method, headers, body }, response);
  return response;
}
