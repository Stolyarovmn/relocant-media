import { createHash } from "node:crypto";
import { get, put, head, BlobPreconditionFailedError } from "@vercel/blob";

export class Conflict extends Error {}

// The adapter never lists stores or constructs public download URLs. All state
// reads bypass the CDN; every replacement is conditional on the origin ETag.
export class PrivateBlobStore {
  constructor(sdk = { get, put, head }, env = process.env) { this.sdk = sdk; this.env = env; }

  credentials() {
    const token = this.env.BLOB_READ_WRITE_TOKEN;
    // An explicit token takes precedence over the SDK's ambient OIDC resolver.
    // Without one, preserve SDK authentication errors rather than masking them.
    return typeof token === "string" && token.length > 0 ? { token } : {};
  }

  async read(path) {
    const result = await this.sdk.get(path, {
      ...this.credentials(), access: "private", useCache: false, abortSignal: AbortSignal.timeout(8_000),
    });
    if (!result) return null;
    if (result.statusCode !== 200 || !result.blob.etag) throw new Error("blob_read_failed");
    return { value: await new Response(result.stream).json(), etag: result.blob.etag };
  }

  async write(path, value, etag = null) {
    try {
      const result = await this.sdk.put(path, JSON.stringify(value), {
        ...this.credentials(), access: "private", addRandomSuffix: false, contentType: "application/json",
        cacheControlMaxAge: 60, abortSignal: AbortSignal.timeout(8_000),
        ...(etag === null ? { allowOverwrite: false } : { allowOverwrite: true, ifMatch: etag }),
      });
      return result.etag;
    } catch (error) {
      if (error instanceof BlobPreconditionFailedError || error?.statusCode === 412 ||
          error?.message === "This blob already exists. Use `allowOverwrite: true` to overwrite it.") {
        throw new Conflict();
      }
      throw error;
    }
  }

  async metadata(path) {
    // head uses the authenticated metadata API; it never downloads Blob content.
    const result = await this.sdk.head(path, {
      ...this.credentials(), abortSignal: AbortSignal.timeout(8_000),
    });
    if (!result || typeof result.etag !== "string" || result.etag.length === 0 ||
        !Number.isSafeInteger(result.size) || result.size < 0 ||
        !(result.uploadedAt instanceof Date) || !Number.isFinite(result.uploadedAt.getTime())) {
      throw new Error("blob_metadata_failed");
    }
    return {
      etag_sha256: createHash("sha256").update(result.etag).digest("hex"),
      uploaded_at: result.uploadedAt.toISOString(), size: result.size,
    };
  }
}

export function namespace(env) {
  if (env.VERCEL_ENV === "production") return "production";
  const id = env.VERCEL_DEPLOYMENT_ID;
  if (typeof id !== "string" || !/^[A-Za-z0-9_-]{1,100}$/u.test(id)) {
    throw new Error("deployment_namespace_missing");
  }
  return `preview/${id}`;
}

export function paths(env) {
  const prefix = namespace(env);
  return { state: `${prefix}/state.json`, config: `${prefix}/config.json` };
}
