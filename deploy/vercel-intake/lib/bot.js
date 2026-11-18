const allowed = new Set(["getChat", "sendMessage", "answerCallbackQuery"]);

// No arbitrary URL, API method or token may be supplied by a Telegram update.
export class ReplyBot {
  constructor(config, canSend, fetcher = fetch) {
    this.config = config;
    this.canSend = canSend;
    this.fetcher = fetcher;
  }

  async call(method, params) {
    if (!this.canSend || !allowed.has(method) ||
        Number(this.config.bot_token.split(":")[0]) !== this.config.bot_id) {
      throw new Error("bot_boundary");
    }
    const response = await this.fetcher(
      `https://api.telegram.org/bot${this.config.bot_token}/${method}`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(params), signal: AbortSignal.timeout(7_000), redirect: "error",
      });
    if (!response.ok) throw new Error("bot_api_failed");
    const body = await response.json();
    if (body.ok !== true) throw new Error("bot_api_failed");
    return body.result;
  }
}
