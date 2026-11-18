import test from "node:test";
import assert from "node:assert/strict";
import { BlobPreconditionFailedError } from "@vercel/blob";
import { Conflict, PrivateBlobStore, paths } from "../lib/store.js";
import { ReplyBot } from "../lib/bot.js";

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
