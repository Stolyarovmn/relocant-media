import { createStore } from "../lib/store.js";
import { Ingress, LIMITS } from "../lib/engine.js";
import { bodyJSON, createDiagnostics, equalSecret, failure, HttpError, send } from "../lib/http.js";

export const config = { api: { bodyParser: false } };

export function handler(dependencies = {}) {
  return async (req, res) => {
    const diagnostics = createDiagnostics("/api/telegram", dependencies.logger);
    try {
      if (req.method !== "POST") throw new HttpError(405, "post_required");
      const env = dependencies.env ?? process.env;
      const ingress = new Ingress({
        store: dependencies.store ?? createStore(env, { fetcher: dependencies.fetcher, now: dependencies.now }),
        ...dependencies, onPhase: diagnostics.setPhase,
      });
      const settings = await ingress.config();
      if (!equalSecret(req.headers?.["x-telegram-bot-api-secret-token"], settings.webhook_secret)) {
        throw new HttpError(401, "unauthorized");
      }
      diagnostics.setPhase("plan");
      const update = await bodyJSON(req, LIMITS.updateBytes);
      send(res, 200, await ingress.receive(update, settings));
    } catch (error) { failure(res, error, diagnostics); }
  };
}

export default handler();
