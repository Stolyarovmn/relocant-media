import test from "node:test";
import assert from "node:assert/strict";
import { BlobPreconditionFailedError } from "@vercel/blob";
import { Conflict, PrivateBlobStore, paths } from "../lib/store.js";
import { ReplyBot } from "../lib/bot.js";

test("явный RW token одинаково передаётся get, put и head при настроенном OIDC", async () => {
  const token = "FAKE_EXPLICIT_RW_TOKEN_DO_NOT_PRINT";
  const env = { BLOB_READ_WRITE_TOKEN: token, VERCEL_OIDC_TOKEN: "FAKE_OIDC", BLOB_STORE_ID: "store_fake" };
  const seen = [];
  const check = (method, options) => {
    assert.equal(options.token, token);
    assert.equal(options.oidcToken, undefined);
    assert.equal(options.storeId, undefined);
    seen.push(method);
  };
  const store = new PrivateBlobStore({
    get: async (_path, options) => {
      check("get", options);
      assert.equal(options.access, "private");
      assert.equal(options.useCache, false);
      return { statusCode: 200, blob: { etag: '"etag"' }, stream: new Response("{}").body };
    },
    put: async (_path, _body, options) => {
      check("put", options);
      assert.equal(options.access, "private");
      assert.equal(options.allowOverwrite, true);
      assert.equal(options.ifMatch, '"etag"');
      return { etag: '"next"' };
    },
    head: async (_path, options) => {
      check("head", options);
      return { etag: '"etag"', size: 2, uploadedAt: new Date("2026-11-18T06:00:00Z"), token };
    },
  }, env);
  await store.read("production/state.json");
  await store.write("production/state.json", {}, '"etag"');
  const metadata = await store.metadata("production/state.json");
  assert.deepEqual(seen, ["get", "put", "head"]);
  assert.ok(!JSON.stringify(metadata).includes(token));
});

test("при отсутствии token ошибки SDK авторизации не маскируются", async () => {
  const authError = new Error("credential_missing_fixture");
  const reject = async (...args) => {
    assert.equal(args.at(-1).token, undefined);
    throw authError;
  };
  const store = new PrivateBlobStore({ get: reject, put: reject, head: reject }, {});
  for (const operation of [() => store.read("production/state.json"),
    () => store.write("production/state.json", {}), () => store.metadata("production/state.json")]) {
    await assert.rejects(operation(), (error) => error === authError);
  }
});

test("Blob adapter всегда private, origin read, CAS overwrite и immutable create", async () => {
  let value = { revision: 1 };
  let etag = '"one"';
  const sdk = {
    get: async (path, options) => {
      assert.equal(path, "production/state.json");
      assert.equal(options.access, "private");
      assert.equal(options.useCache, false);
      return { statusCode: 200, blob: { etag }, stream: new Response(JSON.stringify(value)).body };
    },
    put: async (path, text, options) => {
      assert.equal(options.access, "private");
      assert.equal(options.addRandomSuffix, false);
      assert.equal(options.cacheControlMaxAge, 60);
      if (path.endsWith("config.json")) {
        assert.equal(options.allowOverwrite, false);
        assert.equal(options.ifMatch, undefined);
      } else {
        // The service rejects a conditional replacement without allowOverwrite.
        assert.equal(options.allowOverwrite, true);
        if (options.ifMatch !== etag) throw new BlobPreconditionFailedError();
        value = JSON.parse(text);
        etag = '"two"';
      }
      return { etag };
    },
  };
  const store = new PrivateBlobStore(sdk);
  const read = await store.read("production/state.json");
  assert.equal(read.etag, '"one"');
  await store.write("production/state.json", { revision: 2 }, read.etag);
  await assert.rejects(store.write("production/state.json", { revision: 3 }, read.etag), Conflict);
  await store.write("production/config.json", { metadata: true });
  assert.deepEqual((await store.read("production/state.json")).value, { revision: 2 });
});

test("Blob read не принимает cached 304 или отсутствие ETag", async () => {
  for (const result of [{ statusCode: 304, blob: { etag: "one" } }, { statusCode: 200, blob: {} }]) {
    const store = new PrivateBlobStore({ get: async () => result });
    await assert.rejects(store.read("production/state.json"), /blob_read_failed/u);
  }
});

test("namespace нельзя выбрать через внешний pathname", () => {
  assert.deepEqual(paths({ VERCEL_ENV: "production", VERCEL_DEPLOYMENT_ID: "dpl_any" }), {
    state: "production/state.json", config: "production/config.json",
  });
  assert.deepEqual(paths({ VERCEL_ENV: "preview", VERCEL_DEPLOYMENT_ID: "dpl_test" }), {
    state: "preview/dpl_test/state.json", config: "preview/dpl_test/config.json",
  });
  assert.throws(() => paths({ VERCEL_ENV: "preview", VERCEL_DEPLOYMENT_ID: "../production" }));
});

test("BotAPI boundary ограничивает методы, bot id и preview", async () => {
  const config = { bot_id: 123456, bot_token: "123456:FAKE_TEST_ONLY" };
  const calls = [];
  const fetcher = async (url, request) => {
    calls.push({ url, request });
    assert.equal(request.redirect, "error");
    return { ok: true, json: async () => ({ ok: true, result: { message_id: 123 } }) };
  };
  const bot = new ReplyBot(config, true, fetcher);
  for (const method of ["getChat", "sendMessage", "answerCallbackQuery"]) await bot.call(method, { chat_id: 42 });
  for (const method of ["sendPhoto", "deleteMessage", "setWebhook", "getUpdates", "getMe"]) {
    await assert.rejects(bot.call(method, {}), /bot_boundary/u);
  }
  await assert.rejects(new ReplyBot(config, false, fetcher).call("sendMessage", {}), /bot_boundary/u);
  await assert.rejects(new ReplyBot({ ...config, bot_id: 654321 }, true, fetcher).call("sendMessage", {}), /bot_boundary/u);
  assert.equal(calls.length, 3);
});
