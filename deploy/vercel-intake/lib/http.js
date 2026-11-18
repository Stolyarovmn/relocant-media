import { createHash, timingSafeEqual } from "node:crypto";

export class HttpError extends Error {
  constructor(status, code) { super(code); this.status = status; this.code = code; }
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

export function failure(res, error) {
  // SDK/HTTP exceptions can contain URLs, credentials and raw Telegram text.
  // Deliberately do not log, stringify or return those exceptions.
  send(res, error instanceof HttpError ? error.status : 503, {
    status: "error", error: error instanceof HttpError ? error.code : "temporarily_unavailable",
  });
}
