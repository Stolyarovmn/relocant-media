import { PrivateBlobStore } from "../lib/store.js";
import { Ingress, LIMITS } from "../lib/engine.js";
import { bodyJSON, equalSecret, failure, HttpError, send } from "../lib/http.js";

export const config = { api: { bodyParser: false } };

export function handler(dependencies = {}) {
  return async (req, res) => {
    try {
      if (req.method !== "POST") throw new HttpError(405, "post_required");
      const ingress = new Ingress({ store: new PrivateBlobStore(), ...dependencies });
      const settings = await ingress.config();
      if (!equalSecret(req.headers?.["x-telegram-bot-api-secret-token"], settings.webhook_secret)) {
        throw new HttpError(401, "unauthorized");
      }
      const update = await bodyJSON(req, LIMITS.updateBytes);
      send(res, 200, await ingress.receive(update, settings));
    } catch (error) { failure(res, error); }
  };
}

export default handler();
