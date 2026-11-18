import { failure, HttpError, send } from "../lib/http.js";
import { storageConfigured } from "../lib/store.js";

// Public diagnostics read environment presence only, never Blob data or tokens.
export function handler(env = process.env) {
  return async (req, res) => {
    try {
      if (req.method !== "GET") throw new HttpError(405, "get_required");
      send(res, 200, {
        service: "relocant-intake", mode: env.VERCEL_ENV === "production" ? "production" : "preview",
        configured: storageConfigured(env),
      });
    } catch (error) { failure(res, error); }
  };
}

export default handler();
