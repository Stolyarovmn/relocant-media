import test from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { promisify } from "node:util";
import { LUA_READ, LUA_WRITE, LUA_METADATA, RedisRestStore } from "../lib/redis-store.js";
import { Conflict, paths } from "../lib/store.js";
import { fixture, message, seed } from "./helpers.js";

const container = process.env.REDIS_TEST_CONTAINER;
const execute = promisify(execFile);
const onlyWithRedis = { skip: !container && "Задайте REDIS_TEST_CONTAINER для настоящего Redis" };
const scripts = new Set([LUA_READ, LUA_WRITE, LUA_METADATA]);
const endpoint = "https://ci-only.upstash.io";
const token = "FAKE_REDIS_TOKEN_ONLY_FOR_CI";

async function redis(...command) {
  assert.match(container, /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/u);
  const { stdout, stderr } = await execute("docker", [
    "exec", container, "redis-cli", "--json", ...command.map(String),
  ], { timeout: 10_000, maxBuffer: 2 * 1024 * 1024 });
  assert.equal(stderr, "");
  return JSON.parse(stdout);
}

function bridge(requests) {
  return async (url, request) => {
    assert.equal(new URL(url).origin, endpoint);
    assert.equal(request.method, "POST");
    assert.equal(new Headers(request.headers).get("authorization"), `Bearer ${token}`);
    const command = JSON.parse(request.body);
    // No replica GET or EVAL_RO: the adapter must execute the real production
    // scripts on the primary, including reads and metadata operations.
    assert.equal(command[0], "EVAL");
    assert.ok(scripts.has(command[1]));
    assert.equal(String(command[2]), "1");
    assert.ok(command.every((part) => typeof part === "string" || typeof part === "number"));
    requests.push(command);
    return new Response(JSON.stringify({ result: await redis(...command) }), { status: 200 });
  };
}

async function adapter(t, overrides = {}) {
  let now = 1_800_000_001_000;
  const env = {
    VERCEL_ENV: "preview", VERCEL_DEPLOYMENT_ID: `dpl_ci_${randomUUID()}`,
    INGRESS_STORAGE_BACKEND: "redis", UPSTASH_REDIS_REST_URL: endpoint,
    UPSTASH_REDIS_REST_TOKEN: token, INGRESS_DATA_KEY: Buffer.alloc(32, 7).toString("base64url"),
    INGRESS_BRIDGE_TOKEN: "bridge-fixture", ...overrides,
  };
  const requests = [];
  const fetcher = bridge(requests);
  const store = new RedisRestStore({ env, fetcher, now: () => now });
  const selected = paths(env);
  const keys = Object.values(selected).map((path) => `relocant:intake:v1:${path}`);
  t.after(async () => { await redis("DEL", ...keys); });
  return { store, env, fetcher, requests, selected, keys, advance: () => { now += 1_000; } };
}

test("Redis Lua: создание, конфликт и stale ETag сохраняют прежний снимок", onlyWithRedis, async (t) => {
  const f = await adapter(t);
  assert.equal(await f.store.read(f.selected.state), null);
  const etag = await f.store.write(f.selected.state, { revision: 1 });
  assert.deepEqual(await f.store.read(f.selected.state), { value: { revision: 1 }, etag });
  await assert.rejects(f.store.write(f.selected.state, { revision: 2 }), Conflict);
  f.advance();
  const next = await f.store.write(f.selected.state, { revision: 2 }, etag);
  assert.notEqual(next, etag);
  await assert.rejects(f.store.write(f.selected.state, { revision: 3 }, etag), Conflict);
  assert.deepEqual(await f.store.read(f.selected.state), { value: { revision: 2 }, etag: next });
  assert.equal(await redis("TTL", f.keys[0]), -1);
  assert.ok(f.requests.some((command) => command[1] === LUA_READ));
  assert.ok(f.requests.some((command) => command[1] === LUA_WRITE));
});

test("Redis Lua: из одновременных писателей одной версии побеждает ровно один", onlyWithRedis, async (t) => {
  const f = await adapter(t);
  const before = await f.store.write(f.selected.state, { winner: null });
  const writers = Array.from({ length: 8 }, (_, winner) => ({
    store: new RedisRestStore({ env: f.env, fetcher: f.fetcher }), winner,
  }));
  const outcomes = await Promise.allSettled(writers.map(({ store, winner }) =>
    store.write(f.selected.state, { winner }, before)));
  const successes = outcomes.flatMap((result, index) => result.status === "fulfilled" ? [index] : []);
  assert.equal(successes.length, 1);
  assert.equal(outcomes.filter((result) => result.status === "rejected" &&
    result.reason instanceof Conflict).length, 7);
  const saved = await f.store.read(f.selected.state);
  assert.equal(saved.value.winner, successes[0]);
  assert.equal(saved.etag, outcomes[successes[0]].value);
});

test("Redis Lua: последовательные read и metadata видят последнюю версию и размер", onlyWithRedis, async (t) => {
  const f = await adapter(t);
  let previous = null;
  for (let revision = 1; revision <= 4; revision++) {
    const value = { revision, text: "Текст кириллицей", nested: [revision] };
    const etag = await f.store.write(f.selected.state, value, previous);
    const other = new RedisRestStore({ env: f.env, fetcher: f.fetcher });
    assert.deepEqual(await other.read(f.selected.state), { value, etag });
    const metadata = await other.metadata(f.selected.state);
    assert.equal(metadata.etag_sha256, createHash("sha256").update(etag).digest("hex"));
    assert.equal(metadata.size, Buffer.byteLength(JSON.stringify(value)));
    assert.equal(metadata.uploaded_at, new Date(1_800_000_000_000 + revision * 1_000).toISOString());
    previous = etag;
    f.advance();
  }
  assert.ok(f.requests.some((command) => command[1] === LUA_METADATA));
});

test("Redis Lua: шифротекст закрывает данные, неверный ключ и перенос между путями отвергаются", onlyWithRedis, async (t) => {
  const f = await adapter(t);
  const value = { text: "PRIVATE_READER_TEXT", bot_token: "FAKE_BOT_TOKEN_SECRET" };
  await f.store.write(f.selected.state, value);
  const ciphertext = await redis("HGET", f.keys[0], "value");
  assert.equal(typeof ciphertext, "string");
  assert.ok(!ciphertext.includes(value.text));
  assert.ok(!ciphertext.includes(value.bot_token));
  const wrongKey = new RedisRestStore({
    env: { ...f.env, INGRESS_DATA_KEY: Buffer.alloc(32, 8).toString("base64url") }, fetcher: f.fetcher,
  });
  await assert.rejects(wrongKey.read(f.selected.state));
  assert.equal(await redis("COPY", f.keys[0], f.keys[1]), 1);
  await assert.rejects(f.store.read(f.selected.config));
  const count = f.requests.length;
  await assert.rejects(f.store.read("production/../another/state.json"));
  assert.equal(f.requests.length, count);
  assert.deepEqual((await f.store.read(f.selected.state)).value, value);
});

test("Redis Lua: зашифрованный seed, fake Bot, peek и канонический ACK проходят полный цикл", onlyWithRedis, async (t) => {
  const f = await adapter(t, { VERCEL_ENV: "production" });
  const ingress = await fixture({ store: f.store, env: f.env });
  await ingress.receive(message(1, "Привет"));
  await ingress.receive(message(2, "PRIVATE_READER_QUESTION"));
  await ingress.receive(message(3, "Вопрос или идея"));
  assert.equal(ingress.calls.filter((call) => call.method === "sendMessage").length, 2);
  const peek = await ingress.ingress.transfer({ operation: "peek" });
  assert.deepEqual(peek.events.map((event) => event.sequence), [1, 2, 3]);
  assert.equal(peek.events.at(-1).draft.status, "new");
  assert.equal(peek.events.at(-1).notice.status, "sent");
  const stored = await redis("HGET", f.keys[0], "value");
  assert.ok(!stored.includes("PRIVATE_READER_QUESTION"));
  const config = await redis("HGET", f.keys[1], "value");
  assert.ok(!config.includes(ingress.input.bot_token));
  await ingress.ingress.transfer({ operation: "ack", through: 3,
    snapshot_sha256: "a".repeat(64), open_drafts: [] });
  const saved = await ingress.state();
  assert.equal(saved.ack_sequence, 3);
  assert.deepEqual(saved.events, []);
  assert.deepEqual(saved.cases, []);
  assert.ok(!JSON.stringify(saved).includes("PRIVATE_READER_QUESTION"));
  const writes = f.requests.filter((command) => command[1] === LUA_WRITE).length;
  assert.deepEqual((await ingress.ingress.transfer({ operation: "peek" })).events, []);
  assert.equal(f.requests.filter((command) => command[1] === LUA_WRITE).length, writes);
});

test("Redis Lua: пропуск позиции отклоняется, повторный ACK не меняет снимок", onlyWithRedis, async (t) => {
  const f = await adapter(t);
  const fixtureState = await fixture({ store: f.store, env: f.env, mode: "preview" });
  const { ingress } = fixtureState;
  for (let id = 1; id <= 3; id++) await fixtureState.receive(message(id, "Тестовое событие"));
  const initial = await f.store.read(f.selected.state);
  await assert.rejects(ingress.transfer({ operation: "peek", after: 2 }),
    { status: 409, code: "peek_conflict" });
  await assert.rejects(ingress.transfer({ operation: "ack", through: 2, snapshot_sha256: "a".repeat(64) }),
    { status: 409, code: "ack_conflict" });
  assert.deepEqual(await f.store.read(f.selected.state), initial);
  const first = await ingress.transfer({ operation: "peek", limit: 1 });
  assert.deepEqual(first.events.map(event => event.sequence), [1]);
  await assert.rejects(ingress.transfer({ operation: "peek", after: 2 }),
    { status: 409, code: "peek_conflict" });
  const next = await ingress.transfer({ operation: "peek", after: 1 });
  assert.deepEqual(next.events.map(event => event.sequence), [2, 3]);

  const ack = { operation: "ack", through: 2, snapshot_sha256: "a".repeat(64) };
  await ingress.transfer(ack);
  const saved = await f.store.read(f.selected.state);
  assert.equal(saved.value.ack_sequence, 2);
  assert.deepEqual(saved.value.events.map(event => event.sequence), [3]);
  await ingress.transfer(ack);
  await ingress.transfer({ ...ack, snapshot_sha256: "b".repeat(64), open_drafts: [] });
  assert.deepEqual(await f.store.read(f.selected.state), saved);
});

test("Redis Lua: разрыв в сохранённой очереди не увеличивает курсор и не удаляет события", onlyWithRedis, async (t) => {
  const f = await adapter(t);
  const fixtureState = await fixture({ store: f.store, env: f.env, mode: "preview" });
  for (let id = 1; id <= 3; id++) await fixtureState.receive(message(id, "Тестовое событие"));
  await fixtureState.ingress.transfer({ operation: "peek" });
  const saved = await f.store.read(f.selected.state);
  saved.value.events.splice(1, 1);
  const etag = await f.store.write(f.selected.state, saved.value, saved.etag);
  await assert.rejects(fixtureState.ingress.transfer({ operation: "peek" }),
    { status: 409, code: "peek_conflict" });
  await assert.rejects(fixtureState.ingress.transfer({ operation: "peek", after: 2 }),
    { status: 409, code: "peek_conflict" });
  await assert.rejects(fixtureState.ingress.transfer({
    operation: "ack", through: 3, snapshot_sha256: "a".repeat(64),
  }), { status: 409, code: "ack_conflict" });
  assert.deepEqual(await f.store.read(f.selected.state), { value: saved.value, etag });
});

test("Redis Lua: без отправки кнопка передаётся обычным событием и подтверждается", onlyWithRedis, async (t) => {
  const f = await adapter(t);
  const fixtureState = await fixture({ store: f.store, env: f.env, mode: "preview",
    seed: seed({ legacy_conversations: [42] }) });
  const update = message(1, "Разрешить публикацию");
  await fixtureState.receive(update);
  await fixtureState.receive(update);
  const saved = await f.store.read(f.selected.state);
  assert.deepEqual(saved.value.events, [{ sequence: 1, update }]);
  assert.deepEqual(fixtureState.calls, []);

  // Replay a previously written preview envelope with the old marker.
  saved.value.events[0].receipt_only = true;
  await f.store.write(f.selected.state, saved.value, saved.etag);
  const batch = await fixtureState.ingress.transfer({ operation: "peek" });
  assert.deepEqual(batch.events, [{ sequence: 1, update }]);
  await fixtureState.ingress.transfer({ operation: "ack", through: 1,
    snapshot_sha256: "a".repeat(64) });
  assert.deepEqual((await fixtureState.state()).events, []);
});
