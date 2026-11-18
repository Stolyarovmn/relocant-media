import { createHash, timingSafeEqual } from "node:crypto";

export class HttpError extends Error {
  constructor(status, code) { super(code); this.status = status; this.code = code; }
}

const httpCodes = new Set([
  "body_too_large", "invalid_json", "post_required", "get_required", "unauthorized",
  "reference_exhausted", "queue_capacity", "invalid_seed", "invalid_form", "invalid_seed_case",
  "not_bootstrapped", "concurrent_update", "invalid_bootstrap", "bot_identity", "bootstrap_conflict",
  "attempt_lost", "invalid_update", "update_conflict", "attempt_busy", "event_too_large",
  "unknown_operation", "invalid_peek", "invalid_ack", "invalid_open_drafts", "ack_conflict",
]);
const diagnosticCodes = new Set([
  ...httpCodes, "ECONNRESET", "ECONNREFUSED", "ENOTFOUND", "ETIMEDOUT", "UND_ERR_CONNECT_TIMEOUT",
  "precondition_failed", "forbidden", "bad_request", "store_not_found", "store_suspended",
  "service_unavailable", "rate_limited", "oidc_environment_not_allowed", "not_found",
  "redis_invalid_key", "redis_invalid_endpoint", "redis_invalid_ciphertext", "redis_invalid_record",
  "redis_invalid_response", "redis_response_too_large", "redis_http_failed", "redis_command_failed",
  "redis_missing_credentials", "redis_invalid_path", "redis_request_too_large", "redis_transport_failed",
  "redis_invalid_value", "redis_value_too_large", "redis_decryption_failed", "redis_invalid_etag",
  "redis_credentials_conflict",
]);
const diagnosticTypes = new Set([
  "Error", "TypeError", "SyntaxError", "RangeError", "ReferenceError", "URIError", "EvalError",
  "AggregateError", "DOMException", "HttpError", "Conflict", "BlobError", "BlobAccessError",
  "BlobClientTokenExpiredError", "BlobContentTypeNotAllowedError", "BlobFileTooLargeError",
  "BlobNotFoundError", "BlobPathnameMismatchError", "BlobPreconditionFailedError",
  "BlobRequestAbortedError", "BlobServiceNotAvailable", "BlobServiceRateLimited",
  "BlobStoreNotFoundError", "BlobStoreSuspendedError", "BlobUnknownError",
  "BlobOidcEnvironmentNotAllowedError",
  "RedisStoreError",
]);
const diagnosticPhases = new Set(["config", "read_state", "recover", "plan", "commit", "send"]);
const diagnosticRoutes = new Set(["/api/telegram", "/api/transfer"]);

function safeProperty(value, key) {
  try { return value?.[key]; } catch { return undefined; }
}

export function createDiagnostics(route, logger = (entry) => console.error(entry)) {
  let phase = "config";
  return {
    setPhase(value) { phase = diagnosticPhases.has(value) ? value : "config"; },
    report(error) {
      // BlobError subclasses retain Error.name='Error'; constructor.name is
      // useful only after exact allowlist matching. No arbitrary exception
      // property, message, stack, cause or toJSON is serialized.
      const type = safeProperty(safeProperty(error, "constructor"), "name");
      const code = safeProperty(error, "code");
      const entry = {
        route: diagnosticRoutes.has(route) ? route : "UnknownError", phase,
        error_type: diagnosticTypes.has(type) ? type : "UnknownError",
        error_code: diagnosticCodes.has(code) ? code : "UnknownError",
      };
      try { logger(JSON.stringify(entry)); } catch { /* Diagnostics cannot interrupt the response. */ }
    },
  };
}

export function equalSecret(actual, expected) {
  // Digest before comparing to keep the comparison constant length, including
  // missing or differently sized headers. Never include either input in errors.
  const digest = (value) => createHash("sha256").update(typeof value === "string" ? value : "").digest();
  return timingSafeEqual(digest(actual), digest(expected)) && typeof expected === "string" &&
    expected.length > 0 && typeof actual === "string";
}

export async function bodyJSON(req, maximum) {
  const length = req.headers?.["content-length"];
  if (length !== undefined && (!/^\d+$/u.test(String(length)) || Number(length) > maximum)) {
    throw new HttpError(413, "body_too_large");
  }
  let data;
  if (req.body !== undefined) {
    data = Buffer.isBuffer(req.body) ? req.body : Buffer.from(
      typeof req.body === "string" ? req.body : JSON.stringify(req.body));
  } else {
    const chunks = [];
    let bytes = 0;
    for await (const chunk of req) {
      const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      bytes += buffer.length;
      if (bytes > maximum) throw new HttpError(413, "body_too_large");
      chunks.push(buffer);
    }
    data = Buffer.concat(chunks);
  }
  if (data.length > maximum) throw new HttpError(413, "body_too_large");
  try {
    const value = JSON.parse(data.toString("utf8"));
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error();
    return value;
  } catch { throw new HttpError(400, "invalid_json"); }
}

export function send(res, status, value) {
  res.setHeader("Cache-Control", "private, no-store");
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  if (status === 503) res.setHeader("Retry-After", "5");
  res.statusCode = status;
  res.end(JSON.stringify(value));
}

export function failure(res, error, diagnostics) {
  diagnostics?.report(error);
  // SDK/HTTP exceptions can contain URLs, credentials and raw Telegram text.
  // Return only locally registered error codes, including for HttpError.
  const known = error instanceof HttpError && httpCodes.has(error.code);
  const status = known && [400, 401, 405, 409, 413, 503].includes(error.status) ? error.status : 503;
  send(res, status, {
    status: "error", error: known ? error.code : "temporarily_unavailable",
  });
}
