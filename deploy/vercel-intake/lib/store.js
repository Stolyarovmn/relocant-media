import { get, put, BlobPreconditionFailedError } from "@vercel/blob";

export class Conflict extends Error {}

// The adapter never lists stores or constructs public download URLs. All state
// reads bypass the CDN; every replacement is conditional on the origin ETag.
export class PrivateBlobStore {
  constructor(sdk = { get, put }) { this.sdk = sdk; }

  async read(path) {
    const result = await this.sdk.get(path, {
      access: "private", useCache: false, abortSignal: AbortSignal.timeout(8_000),
    });
    if (!result) return null;
    if (result.statusCode !== 200 || !result.blob.etag) throw new Error("blob_read_failed");
    return { value: await new Response(result.stream).json(), etag: result.blob.etag };
  }

  async write(path, value, etag = null) {
    try {
      const result = await this.sdk.put(path, JSON.stringify(value), {
        access: "private", addRandomSuffix: false, contentType: "application/json",
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
