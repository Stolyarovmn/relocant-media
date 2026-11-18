import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { PrivateBlobStore } from "../lib/store.js";
import { handler as transfer } from "../api/transfer.js";
import { invoke } from "./helpers.js";

for (const mode of ["production", "preview"]) {
  test(`metadata ${mode} использует только head фиксированных paths, без GET/put/BotAPI`, async () => {
    const token = "FAKE_PRIVATE_BLOB_RW_TOKEN_METADATA";
    const secret = "FAKE_PRIVATE_METADATA_CONTENT";
    const env = {
      VERCEL_ENV: mode, VERCEL_DEPLOYMENT_ID: "dpl_metadata_test", INGRESS_BRIDGE_TOKEN: "bridge-fixture",
      BLOB_READ_WRITE_TOKEN: token, VERCEL_OIDC_TOKEN: "FAKE_OIDC", BLOB_STORE_ID: "store_fake",
    };
    const prefix = mode === "production" ? "production" : "preview/dpl_metadata_test";
    const seen = [];
    const forbidden = async () => { assert.fail("metadata не должен читать или изменять Blob содержимое"); };
    const logs = [];
    const store = new PrivateBlobStore({
      get: forbidden, put: forbidden,
      head: async (path, options) => {
        assert.equal(options.token, token);
        seen.push(path);
        assert.ok([`${prefix}/state.json`, `${prefix}/config.json`].includes(path));
        return {
          etag: `"${secret}:${path}"`, uploadedAt: new Date("2026-11-18T06:15:00Z"), size: 123,
          url: `https://private/${token}`, downloadUrl: `https://private/${secret}`, pathname: path,
          text: secret, contentDisposition: secret, cacheControl: secret,
        };
      },
    }, env);
    const route = transfer({ store, env, botFactory: forbidden, logger: (entry) => logs.push(entry) });
    const response = await invoke(route, {
      operation: "metadata", path: "another/namespace/state.json", state: secret, config: token,
    }, { authorization: "Bearer bridge-fixture" });
    assert.equal(response.statusCode, 200);
    assert.deepEqual(seen.sort(), [`${prefix}/config.json`, `${prefix}/state.json`]);
    const expected = (path) => ({
      etag_sha256: createHash("sha256").update(`"${secret}:${path}"`).digest("hex"),
      uploaded_at: "2026-11-18T06:15:00.000Z", size: 123,
    });
    assert.deepEqual(response.data, {
      status: "ready", state: expected(`${prefix}/state.json`), config: expected(`${prefix}/config.json`),
    });
    const output = JSON.stringify({ body: response.data, logs });
    for (const value of [secret, token, prefix, "https://", "downloadUrl", "pathname"]) {
      assert.ok(!output.includes(value));
    }
    assert.deepEqual(logs, []);
    assert.equal(response.headers["Cache-Control"], "private, no-store");
  });
}

test("metadata требует bridge auth и без него не вызывает даже head", async () => {
  let calls = 0;
  const forbidden = async () => { calls++; throw new Error("не должен вызываться"); };
  const env = { VERCEL_ENV: "production", INGRESS_BRIDGE_TOKEN: "bridge-fixture" };
  const store = { metadata: forbidden, read: forbidden, write: forbidden };
  const route = transfer({ store, env, botFactory: forbidden, logger: () => {} });
  assert.equal((await invoke(route, { operation: "metadata" })).statusCode, 401);
  assert.equal((await invoke(route, { operation: "metadata" }, { authorization: "Bearer wrong" })).statusCode, 401);
  const unconfigured = transfer({ store, env: { VERCEL_ENV: "production" }, logger: () => {} });
  assert.equal((await invoke(unconfigured, { operation: "metadata" }, { authorization: "Bearer undefined" })).statusCode, 401);
  assert.equal(calls, 0);
});

test("metadata не возвращает ready при missing/invalid SDK metadata или auth error", async () => {
  for (const result of [null, { etag: "", size: 1, uploadedAt: new Date() },
    { etag: "ok", size: -1, uploadedAt: new Date() },
    { etag: "ok", size: 1, uploadedAt: new Date("invalid") },
    { etag: "ok", size: 1, uploadedAt: "2026-11-18T06:15:00Z" }]) {
    const store = new PrivateBlobStore({ head: async () => result }, {});
    await assert.rejects(store.metadata("production/state.json"), /blob_metadata_failed/u);
  }
  const secret = "FAKE_UNREDACTED_SDK_ERROR";
  const logs = [];
  const store = new PrivateBlobStore({ head: async () => { throw new Error(secret); } }, {});
  const response = await invoke(transfer({
    store, env: { VERCEL_ENV: "production", INGRESS_BRIDGE_TOKEN: "bridge-fixture" },
    logger: (entry) => logs.push(entry),
  }), { operation: "metadata" }, { authorization: "Bearer bridge-fixture" });
  assert.equal(response.statusCode, 503);
  assert.deepEqual(response.data, { status: "error", error: "temporarily_unavailable" });
  assert.ok(!JSON.stringify({ body: response.data, logs }).includes(secret));
});
