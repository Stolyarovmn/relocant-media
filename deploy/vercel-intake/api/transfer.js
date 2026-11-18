import { PrivateBlobStore } from "../lib/store.js";
import { Ingress, LIMITS } from "../lib/engine.js";
import { bodyJSON, equalSecret, failure, HttpError, send } from "../lib/http.js";

export const config = { api: { bodyParser: false } };

export function handler(dependencies = {}) {
  return async (req, res) => {
    try {
      if (req.method !== "POST") throw new HttpError(405, "post_required");
      const env = dependencies.env ?? process.env;
      if (!equalSecret(req.headers?.authorization, env.INGRESS_BRIDGE_TOKEN && `Bearer ${env.INGRESS_BRIDGE_TOKEN}`)) {
        throw new HttpError(401, "unauthorized");
      }
      const input = await bodyJSON(req, LIMITS.bootstrapBytes);
      const ingress = new Ingress({ store: new PrivateBlobStore(), ...dependencies });
      send(res, 200, await ingress.transfer(input));
    } catch (error) { failure(res, error); }
  };
}

export default handler();
