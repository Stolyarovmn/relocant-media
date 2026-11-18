import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { Conflict, createStore, PrivateBlobStore } from "../lib/store.js";
import { RedisStoreError } from "../lib/store-errors.js";
import { RedisRestStore, resolveRedisCredentials, REDIS_LIMITS, REDIS_KEY_PREFIX, LUA_READ, LUA_WRITE, LUA_METADATA } from "../lib/redis-store.js";
import { Ingress, LIMITS } from "../lib/engine.js";
import { handler as transfer } from "../api/transfer.js";
import { handler as telegram } from "../api/telegram.js";
import { handler as health } from "../api/health.js";
import { bootstrap, seed, message, invoke } from "./helpers.js";

const statePath = "production/state.json";
const configPath = "production/config.json";
const stateKey = `${REDIS_KEY_PREFIX}${statePath}`;
const configKey = `${REDIS_KEY_PREFIX}${configPath}`;
const env = () => ({
  VERCEL_ENV: "production", INGRESS_STORAGE_BACKEND: "redis", INGRESS_BRIDGE_TOKEN: "bridge-fixture",
  UPSTASH_REDIS_REST_URL: "https://local-fixture.upstash.io", UPSTASH_REDIS_REST_TOKEN: "FAKE_REDIS_TOKEN_ONLY",
  INGRESS_DATA_KEY: Buffer.alloc(32, 7).toString("base64url"),
});

function transport() {
  const records = new Map();
  const commands = [];
  const fetcher = async (url, options) => {
    assert.equal(url, "https://local-fixture.upstash.io");
    assert.equal(options.method, "POST");
    assert.equal(options.redirect, "error");
    assert.ok(options.signal instanceof AbortSignal);
    assert.equal(options.headers.Authorization, "Bearer FAKE_REDIS_TOKEN_ONLY");
    assert.ok(Buffer.byteLength(options.body) <= REDIS_LIMITS.requestBytes);
    const [command, script, count, key, ...args] = JSON.parse(options.body);
    assert.equal(command, "EVAL");
    assert.equal(count, "1");
    assert.ok([LUA_READ, LUA_WRITE, LUA_METADATA].includes(script));
    commands.push({ script, key, args });
    const current = records.get(key);
    let result;
    if (script === LUA_READ) result = current ? [current.value, current.etag, current.uploaded_at, current.size] : null;
    else if (script === LUA_METADATA) result = current ? [current.etag, current.uploaded_at, current.size] : null;
    else {
      const [expected, value, etag, uploaded_at, size] = args;
      if (expected === "" ? current : current?.etag !== expected) result = [0];
      else { records.set(key, { value, etag, uploaded_at, size }); result = [1, etag]; }
    }
    return new Response(JSON.stringify({ result }));
  };
  return { records, commands, fetcher };
}

function storeFixture(options = {}) {
  const fake = options.fake ?? transport();
  const settings = { ...env(), ...options.env };
  const store = new RedisRestStore({ env: settings, fetcher: fake.fetcher, now: () => 1_800_000_001_000 });
  return { fake, settings, store };
}

test("Redis create/read/CAS/metadata используют EVAL и общий Conflict, plaintext не сохраняется", async () => {
  const { store, fake } = storeFixture();
  assert.equal(store.kind, "redis");
  assert.equal(await store.read(statePath), null);
  const value = { private: "Частный текст и FAKE_BOT_TOKEN", nested: { id: 42 } };
  const etag = await store.write(statePath, value);
  assert.deepEqual(await store.read(statePath), { value, etag });
  await assert.rejects(store.write(statePath, value), Conflict);
  await assert.rejects(store.write(statePath, value, "outdated"), Conflict);
  const next = await store.write(statePath, { changed: true }, etag);
  assert.notEqual(next, etag);
  const metadata = await store.metadata(statePath);
  assert.deepEqual(metadata, {
    etag_sha256: createHash("sha256").update(next).digest("hex"), uploaded_at: "2027-01-15T08:00:01.000Z",
    size: Buffer.byteLength(JSON.stringify({ changed: true })),
  });
  assert.ok(!JSON.stringify(fake.records.get(stateKey)).includes(value.private));
  assert.deepEqual(await store.read(statePath), { value: { changed: true }, etag: next });
  assert.equal(fake.commands.at(-2).script, LUA_METADATA);
});

test("параллельные CAS оставляют ровно одного победителя", async () => {
  const { store } = storeFixture();
  const etag = await store.write(statePath, { version: 1 });
  const outcomes = await Promise.allSettled([
    store.write(statePath, { version: 2 }, etag), store.write(statePath, { version: 3 }, etag),
  ]);
  assert.equal(outcomes.filter((result) => result.status === "fulfilled").length, 1);
  assert.equal(outcomes.find((result) => result.status === "rejected").reason instanceof Conflict, true);
});

test("AES-GCM использует новый IV; неверный ключ, подмена пути и изменения ciphertext отклоняются", async () => {
  const { store, fake } = storeFixture();
  const etag = await store.write(statePath, { text: "Секретный текст" });
  const first = JSON.parse(fake.records.get(stateKey).value);
  await store.write(statePath, { text: "Секретный текст" }, etag);
  const second = JSON.parse(fake.records.get(stateKey).value);
  assert.notEqual(first.iv, second.iv);
  assert.notEqual(first.data, second.data);
  fake.records.set(configKey, { ...fake.records.get(stateKey) });
  await assert.rejects(store.read(configPath), { code: "redis_decryption_failed" });
  const wrong = storeFixture({ fake, env: { INGRESS_DATA_KEY: Buffer.alloc(32, 8).toString("base64url") } }).store;
  await assert.rejects(wrong.read(statePath), { code: "redis_decryption_failed" });
  const original = { ...fake.records.get(stateKey) };
  for (const change of [
    (record) => { record.data += "\n"; }, (record) => { record.data = record.data.replace(/.$/u, "_"); },
    (record) => { record.iv = Buffer.alloc(11).toString("base64"); },
    (record) => { record.tag = Buffer.alloc(16).toString("base64"); },
    (record) => { record.v = 2; }, (record) => { record.extra = "injected"; },
  ]) {
    const record = JSON.parse(original.value);
    change(record);
    fake.records.set(stateKey, { ...original, value: JSON.stringify(record) });
    await assert.rejects(store.read(statePath), { code: "redis_decryption_failed" });
  }
});

test("неправильные endpoints, ключи, backend и paths отклоняются до fetch", async () => {
  const invalidUrls = ["http://local-fixture.upstash.io", "https://upstash.io", "https://evil.example",
    "https://local-fixture.upstash.io:443", "https://token@local-fixture.upstash.io", "https://local-fixture.upstash.io?a=1",
    "https://local-fixture.upstash.io#part", "https://local-fixture.upstash.io/command", "https://local-fixture.upstash.io.evil.test"];
  for (const url of invalidUrls) {
    assert.throws(() => storeFixture({ env: { UPSTASH_REDIS_REST_URL: url } }), { code: "redis_invalid_endpoint" });
  }
  for (const key of [undefined, "", "a".repeat(42), "a".repeat(44), "a".repeat(42) + "+", Buffer.alloc(31).toString("base64url"), "A".repeat(42) + "B"]) {
    assert.throws(() => storeFixture({ env: { INGRESS_DATA_KEY: key } }), { code: "redis_invalid_key" });
  }
  assert.throws(() => storeFixture({ env: { UPSTASH_REDIS_REST_TOKEN: "bad\r\nAuthorization" } }), { code: "redis_missing_credentials" });
  const { store, fake } = storeFixture();
  await assert.rejects(store.read("preview/foreign/state.json"), { code: "redis_invalid_path" });
  await assert.rejects(store.write("../production/state.json", {}), { code: "redis_invalid_path" });
  await assert.rejects(store.metadata("production/other.json"), { code: "redis_invalid_path" });
  assert.equal(fake.commands.length, 0);
  assert.throws(() => createStore({ INGRESS_STORAGE_BACKEND: "unknown" }), /storage_backend_invalid/u);
  assert.equal(createStore({}) instanceof PrivateBlobStore, true);
  assert.equal(createStore(env()).kind, "redis");
});

test("UPSTASH и KV aliases выбираются только полными согласованными парами, health использует тот же resolver", async () => {
  const primary = env();
  const marketplace = {
    ...env(), UPSTASH_REDIS_REST_URL: undefined, UPSTASH_REDIS_REST_TOKEN: undefined,
    KV_REST_API_URL: primary.UPSTASH_REDIS_REST_URL, KV_REST_API_TOKEN: primary.UPSTASH_REDIS_REST_TOKEN,
  };
  for (const settings of [primary, marketplace, { ...primary, KV_REST_API_URL: primary.UPSTASH_REDIS_REST_URL,
    KV_REST_API_TOKEN: primary.UPSTASH_REDIS_REST_TOKEN }]) {
    assert.deepEqual(resolveRedisCredentials(settings), {
      url: primary.UPSTASH_REDIS_REST_URL, token: primary.UPSTASH_REDIS_REST_TOKEN,
    });
    const fake = transport();
    const store = new RedisRestStore({ env: settings, fetcher: fake.fetcher });
    assert.equal(await store.read(statePath), null);
    assert.equal((await invoke(health(settings), undefined, {}, "GET")).data.configured, true);
  }
  for (const settings of [
    { ...marketplace, KV_REST_API_TOKEN: undefined, UPSTASH_REDIS_REST_TOKEN: primary.UPSTASH_REDIS_REST_TOKEN },
    { ...marketplace, UPSTASH_REDIS_REST_URL: primary.UPSTASH_REDIS_REST_URL },
    { ...primary, KV_REST_API_URL: primary.UPSTASH_REDIS_REST_URL },
    { ...primary, KV_REST_API_URL: "https://other-fixture.upstash.io", KV_REST_API_TOKEN: primary.UPSTASH_REDIS_REST_TOKEN },
    { ...primary, KV_REST_API_URL: primary.UPSTASH_REDIS_REST_URL, KV_REST_API_TOKEN: "ANOTHER_FAKE_TOKEN" },
  ]) {
    assert.throws(() => new RedisRestStore({ env: settings }), RedisStoreError);
    assert.equal((await invoke(health(settings), undefined, {}, "GET")).data.configured, false);
  }
});

test("oversize plaintext и транспорт с base64 отклоняются до записи", async () => {
  const { store, fake } = storeFixture();
  await assert.rejects(store.write(statePath, { body: "x".repeat(REDIS_LIMITS.plainBytes) }), { code: "redis_value_too_large" });
  await assert.rejects(store.write(statePath, { body: "x".repeat(7 * 1024 * 1024) }), { code: "redis_request_too_large" });
  assert.equal(fake.commands.length, 0);
});

test("неограниченные ответы, Redis errors, JSON и сетевые исключения заменяются фиксированными ошибками", async () => {
  const cases = [
    [async () => new Response("private", { status: 403 }), "redis_http_failed"],
    [async () => new Response(JSON.stringify({ error: "private secret body" })), "redis_command_failed"],
    [async () => new Response("notjson private"), "redis_invalid_response"],
    [async () => new Response("", { headers: { "content-length": String(REDIS_LIMITS.responseBytes + 1) } }), "redis_response_too_large"],
    [async () => new Response(new Uint8Array(REDIS_LIMITS.responseBytes + 1)), "redis_response_too_large"],
    [async () => { throw new Error("https://private/token secret"); }, "redis_transport_failed"],
  ];
  for (const [fetcher, code] of cases) {
    const store = new RedisRestStore({ env: env(), fetcher });
    await assert.rejects(store.read(statePath), (error) => {
      assert.equal(error instanceof RedisStoreError, true);
      assert.equal(error.code, code);
      assert.equal(error.message, code);
      return true;
    });
  }
});

test("storage-info читает оба ключа, отличает пустое и partial состояние; не вызывает BotAPI", async () => {
  const { store, fake, settings } = storeFixture();
  let sends = 0;
  const route = transfer({ store, env: settings, botFactory: () => { sends++; throw new Error("unexpected"); }, logger: () => {} });
  const body = { operation: "storage-info", backend: "blob", namespace: "foreign" };
  assert.equal((await invoke(route, body)).statusCode, 401);
  assert.equal(fake.commands.length, 0);
  const auth = { authorization: "Bearer bridge-fixture" };
  assert.deepEqual((await invoke(route, body, auth)).data, {
    status: "ready", backend: "redis", namespace: "production", initialized: false,
  });
  const seedHash = "a".repeat(64);
  await store.write(configPath, { seed_sha256: seedHash, bot_token: "FAKE_PRIVATE_BOT_TOKEN" });
  assert.deepEqual((await invoke(route, body, auth)).data, {
    status: "ready", backend: "redis", namespace: "production", initialized: true,
    seed_sha256: seedHash, mode: "incomplete",
  });
  await store.write(statePath, { seed_sha256: seedHash, mode: "active" });
  assert.deepEqual((await invoke(route, body, auth)).data, {
    status: "ready", backend: "redis", namespace: "production", initialized: true,
    seed_sha256: seedHash, mode: "active",
  });
  const config = fake.records.get(configKey);
  fake.records.delete(configKey);
  assert.deepEqual((await invoke(route, body, auth)).data, {
    status: "ready", backend: "redis", namespace: "production", initialized: true,
    seed_sha256: seedHash, mode: "incomplete",
  });
  fake.records.set(configKey, config);
  await store.write(statePath, { seed_sha256: "b".repeat(64), mode: "active" }, (await store.read(statePath)).etag);
  assert.equal((await invoke(route, body, auth)).statusCode, 503);
  assert.equal(sends, 0);
});

test("API selection Redis работает без Blob credentials и health не открывает соединений", async () => {
  const fake = transport();
  const settings = env();
  const deps = { env: settings, fetcher: fake.fetcher, logger: () => {}, botFactory: () => ({ call: async () => ({ message_id: 999 }) }) };
  const auth = { authorization: "Bearer bridge-fixture" };
  assert.equal((await invoke(health(settings), undefined, {}, "GET")).data.configured, true);
  assert.equal((await invoke(health({ ...settings, INGRESS_DATA_KEY: "" }), undefined, {}, "GET")).data.configured, false);
  assert.equal(fake.commands.length, 0);
  assert.equal((await invoke(transfer(deps), bootstrap(seed()), auth)).statusCode, 200);
  assert.equal((await invoke(telegram(deps), message(1, "Частное сообщение"), {
    "x-telegram-bot-api-secret-token": "FAKE_WEBHOOK_SECRET_ONLY_FOR_LOCAL_TESTS",
  })).statusCode, 200);
  const peek = await invoke(transfer(deps), { operation: "peek" }, auth);
  assert.equal(peek.data.events.length, 1);
  assert.equal(peek.data.events[0].notice.status, "sent");
});

test("preview Redis namespace изолирован, активный режим preview не вызывает BotAPI", async () => {
  const { fake } = storeFixture();
  const settings = { ...env(), VERCEL_ENV: "preview", VERCEL_DEPLOYMENT_ID: "dpl_redis" };
  let sends = 0;
  const deps = { env: settings, fetcher: fake.fetcher, logger: () => {}, botFactory: () => { sends++; throw new Error("preview_network"); } };
  const auth = { authorization: "Bearer bridge-fixture" };
  await invoke(transfer(deps), bootstrap(seed(), "active"), auth);
  const response = await invoke(telegram(deps), message(1, "Привет"), {
    "x-telegram-bot-api-secret-token": "FAKE_WEBHOOK_SECRET_ONLY_FOR_LOCAL_TESTS",
  });
  assert.equal(response.statusCode, 200);
  assert.equal(sends, 0);
  assert.ok(fake.commands.every((command) => command.key.startsWith(`${REDIS_KEY_PREFIX}preview/dpl_redis/`)));
});

test("transport reserve отклоняет prepared до CAS/send; допустимый reserve оставляет место finalize", async () => {
  for (const paddingSize of [6_980_000, 6_800_000]) {
    const { store, fake, settings } = storeFixture();
    let sends = 0;
    const ingress = new Ingress({ store, env: settings, botFactory: () => ({ call: async () => { sends++; return { message_id: 99 }; } }) });
    await ingress.bootstrap(bootstrap());
    const original = await store.read(statePath);
    original.value.padding = "x".repeat(paddingSize);
    await store.write(statePath, original.value, original.etag);
    const config = await ingress.config();
    const before = { ...fake.records.get(stateKey) };
    const writes = fake.commands.filter((item) => item.script === LUA_WRITE).length;
    if (paddingSize > 6_900_000) {
      await assert.rejects(ingress.receive(message(1, "Привет"), config), { code: "redis_request_too_large" });
      assert.equal(sends, 0);
      assert.deepEqual(fake.records.get(stateKey), before);
      assert.equal(fake.commands.filter((item) => item.script === LUA_WRITE).length, writes);
    } else {
      await ingress.receive(message(1, "Привет"), config);
      const state = (await store.read(statePath)).value;
      assert.equal(sends, 1);
      assert.equal(state.busy, null);
      assert.equal(state.events[0].notice.status, "sent");
    }
  }
});

test("bootstrap проверяет transport reserve до создания config/state", async () => {
  const { store, fake, settings } = storeFixture();
  const ingress = new Ingress({ store, env: settings });
  const value = seed();
  value.form.menu = "x".repeat(6_980_000);
  await assert.rejects(ingress.bootstrap(bootstrap(value)), { code: "redis_request_too_large" });
  assert.equal(fake.commands.length, 0);
  assert.equal(fake.records.size, 0);
});
