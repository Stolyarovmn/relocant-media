import { failure, HttpError, send } from "../lib/http.js";

// Public diagnostics read environment presence only, never Blob data or tokens.
export function handler(env = process.env) {
  return async (req, res) => {
    try {
      if (req.method !== "GET") throw new HttpError(405, "get_required");
      send(res, 200, {
        service: "relocant-intake", mode: env.VERCEL_ENV === "production" ? "production" : "preview",
        configured: Boolean(env.INGRESS_BRIDGE_TOKEN && (env.BLOB_READ_WRITE_TOKEN || env.BLOB_STORE_ID)),
      });
    } catch (error) { failure(res, error); }
  };
}

export default handler();
