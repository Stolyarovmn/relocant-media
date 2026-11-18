import test from "node:test";
import assert from "node:assert/strict";
import { Readable } from "node:stream";
import { handler as telegram } from "../api/telegram.js";
import { handler as transfer } from "../api/transfer.js";
import { handler as health } from "../api/health.js";
import { bodyJSON, equalSecret } from "../lib/http.js";
import { LIMITS } from "../lib/engine.js";
import { fixture, message, invoke, bootstrap, FakeStore } from "./helpers.js";

test("оба endpoint принимают только POST; auth не возвращает содержимое или секреты", async () => {
  const f = await fixture();
  for (const route of [telegram(f.deps), transfer(f.deps)]) {
    assert.equal((await invoke(route, {}, {}, "GET")).statusCode, 405);
    const response = await invoke(route, { text: "Не показывать" });
    assert.equal(response.statusCode, 401);
    assert.equal(JSON.stringify(response.data).includes("Не показывать"), false);
    assert.equal(response.headers["Cache-Control"], "private, no-store");
  }
  assert.equal(f.calls.length, 0);
  assert.equal((await f.state()).events.length, 0);
  assert.equal(equalSecret("a", "long"), false);
  assert.equal(equalSecret(undefined, undefined), false);
});

test("без bridge token transfer fail closed, даже при заголовке Bearer undefined", async () => {
  const f = await fixture({ env: { VERCEL_ENV: "production" } });
  const result = await invoke(transfer(f.deps), { operation: "status" }, { authorization: "Bearer undefined" });
  assert.equal(result.statusCode, 401);
});

test("public health не читает private state и раскрывает только наличие настройки", async () => {
  assert.deepEqual((await invoke(health({ VERCEL_ENV: "preview" }), undefined, {}, "GET")).data, {
    service: "relocant-intake", mode: "preview", configured: false,
  });
  assert.deepEqual((await invoke(health({ VERCEL_ENV: "production", INGRESS_BRIDGE_TOKEN: "private", BLOB_STORE_ID: "store" }),
    undefined, {}, "GET")).data, { service: "relocant-intake", mode: "production", configured: true });
});

test("HTTP bootstrap требует bearer, сохраняет private config и отдаёт только метаданные", async () => {
  const store = new FakeStore();
  const env = { VERCEL_ENV: "production", INGRESS_BRIDGE_TOKEN: "bridge-fixture" };
  const route = transfer({ store, env });
  const response = await invoke(route, bootstrap(), { authorization: "Bearer bridge-fixture" });
  assert.equal(response.statusCode, 200);
  assert.equal(response.data.status, "ready");
  assert.equal(response.data.can_send, true);
  assert.equal(JSON.stringify(response.data).includes("FAKE_TOKEN"), false);
  assert.equal(JSON.stringify(response.data).includes("WEBHOOK_SECRET"), false);
  const webhook = await invoke(telegram({ store, env }), message(1), { "x-telegram-bot-api-secret-token": "wrong" });
  assert.equal(webhook.statusCode, 401);
});

test("webhook 512KB cap и invalid JSON не создают receipt", async () => {
  const f = await fixture();
  const route = telegram(f.deps);
  const auth = { "x-telegram-bot-api-secret-token": f.config.webhook_secret };
  assert.equal((await invoke(route, { update_id: 1, text: "x".repeat(LIMITS.updateBytes) }, auth)).statusCode, 413);
  assert.equal((await invoke(route, message(1), { ...auth, "content-length": String(LIMITS.updateBytes + 1) })).statusCode, 413);
  assert.equal((await invoke(route, "{broken", auth)).statusCode, 400);
  assert.equal((await invoke(route, [], auth)).statusCode, 400);
  assert.equal((await f.state()).events.length, 0);
  const response = await invoke(route, message(2, "Привет"), auth);
  assert.equal(response.statusCode, 200);
  assert.deepEqual(response.data, { status: "ready" });
});

test("stream parser останавливается на превышении cap", async () => {
  const req = Readable.from([Buffer.from("{"), Buffer.alloc(12)]);
  req.headers = {};
  await assert.rejects(bodyJSON(req, 10), { status: 413 });
});

test("HTTP peek отдаёт completed envelopes; ack и status не раскрывают private text", async () => {
  const f = await fixture();
  const auth = { authorization: "Bearer bridge-fixture" };
  const route = transfer(f.deps);
  await f.receive(message(1, "Конфиденциальный текст"));
  const peek = await invoke(route, { operation: "peek", limit: 30 }, auth);
  assert.equal(peek.statusCode, 200);
  assert.equal(peek.data.events[0].draft.payload.text, "Конфиденциальный текст");
  assert.equal((await invoke(route, { operation: "peek", limit: 31 }, auth)).statusCode, 400);
  assert.equal((await invoke(route, { operation: "peek", after: -1 }, auth)).statusCode, 400);
  const response = await invoke(route, { operation: "status" }, auth);
  assert.equal(response.statusCode, 200);
  assert.equal(JSON.stringify(response.data).includes("Конфиденциальный"), false);
  assert.equal(response.data.pending_count, 1);
  assert.equal((await invoke(route, { operation: "ack", through: 1, snapshot_sha256: "a".repeat(64) }, auth)).statusCode, 200);
  assert.equal((await f.state()).events.length, 0);
  assert.equal((await f.state()).cases.length, 1);
});

test("ошибки storage sanitised и не утекли URL, token или сырой update", async () => {
  const store = { read: async () => { throw new Error("https://secret/token private raw text"); } };
  const route = telegram({ store, env: { VERCEL_ENV: "production" } });
  const response = await invoke(route, message(1));
  assert.equal(response.statusCode, 503);
  assert.deepEqual(response.data, { status: "error", error: "temporarily_unavailable" });
  assert.equal(response.headers["Retry-After"], "5");
});
