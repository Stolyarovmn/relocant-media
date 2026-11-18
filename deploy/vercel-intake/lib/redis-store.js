import { createCipheriv, createDecipheriv, createHash, randomBytes, randomUUID } from "node:crypto";
import { Conflict, RedisStoreError } from "./store-errors.js";
import { paths } from "./store-paths.js";

export const REDIS_LIMITS = Object.freeze({
  plainBytes: 8 * 1024 * 1024, requestBytes: 9 * 1024 * 1024,
  responseBytes: 9 * 1024 * 1024, timeoutMs: 8_000,
});
export const REDIS_KEY_PREFIX = "relocant:intake:v1:";

// EVAL (not EVAL_RO) pins even these read-only scripts to the primary. Never
// substitute REST GET/HMGET or eventual read replicas for the CAS read.
export const LUA_READ = `
if redis.call('EXISTS', KEYS[1]) == 0 then return nil end
return redis.call('HMGET', KEYS[1], 'value', 'etag', 'uploaded_at', 'size')
`;
export const LUA_METADATA = `
if redis.call('EXISTS', KEYS[1]) == 0 then return nil end
return redis.call('HMGET', KEYS[1], 'etag', 'uploaded_at', 'size')
`;
export const LUA_WRITE = `
local current = redis.call('HGET', KEYS[1], 'etag')
if ARGV[1] == '' then
  if redis.call('EXISTS', KEYS[1]) ~= 0 then return {0} end
elseif not current or current ~= ARGV[1] then
  return {0}
end
redis.call('HSET', KEYS[1], 'value', ARGV[2], 'etag', ARGV[3], 'uploaded_at', ARGV[4], 'size', ARGV[5])
return {1, ARGV[3]}
`;

function fail(code) { throw new RedisStoreError(code); }

function dataKey(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{43}$/u.test(value)) fail("redis_invalid_key");
  const key = Buffer.from(value, "base64url");
  if (key.length !== 32 || key.toString("base64url") !== value) fail("redis_invalid_key");
  return key;
}

function endpoint(value) {
  if (typeof value !== "string" ||
      !/^https:\/\/(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+upstash\.io\/?$/iu.test(value)) {
    fail("redis_invalid_endpoint");
  }
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port || url.search ||
        url.hash || url.pathname !== "/") fail("redis_invalid_endpoint");
    return url.origin;
  } catch { fail("redis_invalid_endpoint"); }
}

export function resolveRedisCredentials(env) {
  const pair = (url, token) => {
    if (url === undefined && token === undefined) return null;
    if (typeof url !== "string" || url.length === 0 || typeof token !== "string" ||
        !/^[\x21-\x7e]{1,8192}$/u.test(token)) fail("redis_missing_credentials");
    return { url: endpoint(url), token };
  };
  const primary = pair(env.UPSTASH_REDIS_REST_URL, env.UPSTASH_REDIS_REST_TOKEN);
  const marketplace = pair(env.KV_REST_API_URL, env.KV_REST_API_TOKEN);
  if (primary && marketplace && (primary.url !== marketplace.url || primary.token !== marketplace.token)) {
    fail("redis_credentials_conflict");
  }
  if (!primary && !marketplace) fail("redis_missing_credentials");
  return primary ?? marketplace;
}

function strictBase64(value, expectedLength) {
  if (typeof value !== "string" || !value.length || value.length > REDIS_LIMITS.responseBytes ||
      value.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/u.test(value)) {
    fail("redis_invalid_ciphertext");
  }
  const decoded = Buffer.from(value, "base64");
  if (decoded.toString("base64") !== value || (expectedLength !== undefined && decoded.length !== expectedLength)) {
    fail("redis_invalid_ciphertext");
  }
  return decoded;
}

function recordMetadata(result) {
  if (!Array.isArray(result) || result.length !== 3 ||
      typeof result[0] !== "string" || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/u.test(result[0]) ||
      typeof result[1] !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u.test(result[1]) ||
      !Number.isFinite(Date.parse(result[1])) || new Date(result[1]).toISOString() !== result[1] ||
      typeof result[2] !== "string" || !/^(?:0|[1-9]\d*)$/u.test(result[2])) fail("redis_invalid_record");
  const size = Number(result[2]);
  if (!Number.isSafeInteger(size) || size < 0 || size > REDIS_LIMITS.plainBytes) fail("redis_invalid_record");
  return { etag: result[0], uploaded_at: result[1], size };
}

async function responseJSON(response) {
  if (!response?.ok) fail("redis_http_failed");
  const length = response.headers?.get("content-length");
  if (length !== null && length !== undefined &&
      (!/^\d+$/u.test(length) || Number(length) > REDIS_LIMITS.responseBytes)) fail("redis_response_too_large");
  if (!response.body || typeof response.body.getReader !== "function") fail("redis_invalid_response");
  const reader = response.body.getReader();
  const chunks = [];
  let size = 0;
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      size += chunk.value.byteLength;
      if (size > REDIS_LIMITS.responseBytes) {
        await reader.cancel();
        fail("redis_response_too_large");
      }
      chunks.push(Buffer.from(chunk.value));
    }
  } finally { reader.releaseLock(); }
  let parsed;
  try { parsed = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(Buffer.concat(chunks))); }
  catch { fail("redis_invalid_response"); }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed) ||
      !Object.hasOwn(parsed, "result") || Object.hasOwn(parsed, "error")) fail("redis_command_failed");
  return parsed.result;
}

export class RedisRestStore {
  kind = "redis";

  constructor({ env = process.env, fetcher = fetch, now = Date.now } = {}) {
    const credentials = resolveRedisCredentials(env);
    this.url = credentials.url;
    this.token = credentials.token;
    this.key = dataKey(env.INGRESS_DATA_KEY);
    this.allowedPaths = new Set(Object.values(paths(env)));
    this.fetcher = fetcher;
    this.now = now;
  }

  redisKey(path) {
    if (!this.allowedPaths.has(path)) fail("redis_invalid_path");
    return `${REDIS_KEY_PREFIX}${path}`;
  }

  async eval(script, path, args = []) {
    const body = JSON.stringify(["EVAL", script, "1", this.redisKey(path), ...args]);
    if (Buffer.byteLength(body) > REDIS_LIMITS.requestBytes) fail("redis_request_too_large");
    try {
      const response = await this.fetcher(this.url, {
        method: "POST", headers: { Authorization: `Bearer ${this.token}`, "Content-Type": "application/json" },
        body, signal: AbortSignal.timeout(REDIS_LIMITS.timeoutMs), redirect: "error",
      });
      return await responseJSON(response);
    } catch (error) {
      if (error instanceof RedisStoreError) throw error;
      fail("redis_transport_failed");
    }
  }

  assertCapacity(value, reserveBytes = 0) {
    let json;
    try { json = JSON.stringify(value); } catch { fail("redis_invalid_value"); }
    if (typeof json !== "string" || !Number.isSafeInteger(reserveBytes) || reserveBytes < 0) fail("redis_invalid_value");
    const size = Buffer.byteLength(json) + reserveBytes;
    if (size > REDIS_LIMITS.plainBytes) fail("redis_value_too_large");
    // Account for AES-GCM base64 expansion, JSON escaping of the small envelope
    // and the largest permitted CAS token/path before a prepared send is stored.
    const longestPath = [...this.allowedPaths].sort((a, b) => b.length - a.length)[0];
    const envelope = JSON.stringify({ v: 1, iv: "A".repeat(16), data: "", tag: "A".repeat(24) });
    const overhead = Buffer.byteLength(JSON.stringify([
      "EVAL", LUA_WRITE, "1", this.redisKey(longestPath), "x".repeat(256), envelope,
      "x".repeat(36), "2026-01-01T00:00:00.000Z", String(size),
    ]));
    if (overhead + 4 * Math.ceil(size / 3) > REDIS_LIMITS.requestBytes) fail("redis_request_too_large");
  }

  encrypt(path, value) {
    let json;
    try { json = JSON.stringify(value); } catch { fail("redis_invalid_value"); }
    if (typeof json !== "string") fail("redis_invalid_value");
    const plain = Buffer.from(json);
    if (plain.length > REDIS_LIMITS.plainBytes) fail("redis_value_too_large");
    const iv = randomBytes(12);
    const cipher = createCipheriv("aes-256-gcm", this.key, iv);
    cipher.setAAD(Buffer.from(path));
    const encrypted = Buffer.concat([cipher.update(plain), cipher.final()]);
    return {
      size: plain.length,
      value: JSON.stringify({ v: 1, iv: iv.toString("base64"), data: encrypted.toString("base64"), tag: cipher.getAuthTag().toString("base64") }),
    };
  }

  decrypt(path, record, size) {
    try {
      if (typeof record !== "string" || Buffer.byteLength(record) > REDIS_LIMITS.responseBytes) fail("redis_invalid_ciphertext");
      const value = JSON.parse(record);
      if (!value || value.v !== 1 || Object.keys(value).sort().join(",") !== "data,iv,tag,v") fail("redis_invalid_ciphertext");
      const iv = strictBase64(value.iv, 12);
      const tag = strictBase64(value.tag, 16);
      const encrypted = strictBase64(value.data, size);
      const decipher = createDecipheriv("aes-256-gcm", this.key, iv);
      decipher.setAAD(Buffer.from(path));
      decipher.setAuthTag(tag);
      const plain = Buffer.concat([decipher.update(encrypted), decipher.final()]);
      if (plain.length !== size || plain.length > REDIS_LIMITS.plainBytes) fail("redis_invalid_ciphertext");
      return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(plain));
    } catch { fail("redis_decryption_failed"); }
  }

  async read(path) {
    const result = await this.eval(LUA_READ, path);
    if (result === null) return null;
    if (!Array.isArray(result) || result.length !== 4) fail("redis_invalid_record");
    const metadata = recordMetadata(result.slice(1));
    return { value: this.decrypt(path, result[0], metadata.size), etag: metadata.etag };
  }

  async write(path, value, expected = null) {
    this.redisKey(path);
    if (expected !== null && (typeof expected !== "string" || expected.length === 0 || expected.length > 256)) fail("redis_invalid_etag");
    this.assertCapacity(value);
    const encrypted = this.encrypt(path, value);
    const etag = randomUUID();
    const uploadedAt = new Date(this.now()).toISOString();
    const result = await this.eval(LUA_WRITE, path, [expected ?? "", encrypted.value, etag, uploadedAt, String(encrypted.size)]);
    if (Array.isArray(result) && result.length === 1 && result[0] === 0) throw new Conflict();
    if (!Array.isArray(result) || result.length !== 2 || result[0] !== 1 || result[1] !== etag) fail("redis_invalid_response");
    return etag;
  }

  async metadata(path) {
    const metadata = recordMetadata(await this.eval(LUA_METADATA, path));
    return {
      etag_sha256: createHash("sha256").update(metadata.etag).digest("hex"),
      uploaded_at: metadata.uploaded_at, size: metadata.size,
    };
  }
}
