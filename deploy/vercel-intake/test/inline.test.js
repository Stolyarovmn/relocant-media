import test from "node:test";
import assert from "node:assert/strict";
import { answerInline, inlineItems, inlineMatches, inlineResult, resetInlineCache } from "../lib/inline.js";

const now = Date.parse("2026-11-22T10:00:00+01:00");
const water = "event_0123456789abcdef1234";
const concert = "event_0123456789abcdef1235";
const incidents = { version: 1, incidents: [
  { id: water, kind: "water", title: "Без воды на Врачаре", starts_at: "2026-11-22T08:00:00+01:00", ends_at: "2026-11-22T16:00:00+01:00", reported_scope: "Врачар" },
  { id: "event_0123456789abcdef9999", kind: "power", title: "Старое отключение", ends_at: "2026-11-21T16:00:00+01:00" },
  { id: "bad", kind: "water", title: "Без ID" },
] };
const events = { version: 1, events: [
  { id: concert, kind: "event", title: "Джаз <вечер>", starts_at: "2026-11-24T20:00:00+01:00", expires_at: "2026-11-25T00:00:00+01:00", venue: { name: "Дом омладине" } },
  { id: "event_0123456789abcdef7777", kind: "event", sponsored: true, title: "Реклама джаза", starts_at: "2026-11-24T20:00:00+01:00" },
] };

test("inline берёт только актуальные карточки без рекламы", () => {
  const items = inlineItems(incidents, events, now);
  assert.deepEqual(items.map((item) => item.id), [water, concert]);
  assert.deepEqual(inlineMatches(items, `places_${concert}`).map((item) => item.id), [concert]);
  assert.deepEqual(inlineMatches(items, "ДЖАЗ омладине").map((item) => item.id), [concert]);
  assert.deepEqual(inlineMatches(items, "врачаре").map((item) => item.id), [water]);
  assert.equal(inlineMatches(items, "").length, 2);
  assert.deepEqual(inlineMatches(items, "event_0000000000000000dead"), []);
});

test("карточка ведёт в миниапп и экранирует HTML", () => {
  const [, item] = inlineItems(incidents, events, now);
  const result = inlineResult(item);
  assert.equal(result.reply_markup.inline_keyboard[0][0].url, `https://t.me/relocant_media_belgrade_bot?startapp=places_${concert}`);
  assert.match(result.input_message_content.message_text, /<b>Джаз &lt;вечер&gt;<\/b>/u);
  assert.equal(result.input_message_content.parse_mode, "HTML");
});

test("ответ на inline_query читает публичные JSON и не падает на ошибке Telegram", async () => {
  resetInlineCache();
  const urls = [];
  const fetcher = async (url) => {
    urls.push(url);
    return new Response(JSON.stringify(url.endsWith("incidents.json") ? incidents : events));
  };
  const calls = [];
  const bot = { call: async (method, params) => { calls.push([method, params]); return true; } };
  const update = { update_id: 1, inline_query: { id: "q1", query: water } };
  assert.deepEqual(await answerInline(update, bot, { fetcher, now }), { status: "ready", results: 1 });
  assert.equal(calls[0][0], "answerInlineQuery");
  assert.equal(calls[0][1].results[0].id, water);
  assert.ok(urls.every((url) => url.startsWith("https://stolyarovmn.github.io/relocant-media/map/")));
  await answerInline({ inline_query: { id: "q2", query: "ничего" } }, bot, { fetcher, now });
  assert.equal(calls[1][1].results.length, 0);
  assert.equal(calls[1][1].button.start_parameter, "map");
  assert.equal(urls.length, 2, "второй запрос берёт данные из кэша");
  const failing = { call: async () => { throw new Error("bot_api_failed"); } };
  assert.deepEqual(await answerInline(update, failing, { fetcher, now }), { status: "inline_failed" });
});

test("webhook отвечает на inline_query без записи состояния, превью не отвечает", async () => {
  const { fixture, invoke } = await import("./helpers.js");
  const { handler: telegram } = await import("../api/telegram.js");
  resetInlineCache();
  const headers = { "x-telegram-bot-api-secret-token": "FAKE_WEBHOOK_SECRET_ONLY_FOR_LOCAL_TESTS" };
  const fetcher = async (url) => new Response(JSON.stringify(url.endsWith("incidents.json") ? incidents : events));
  for (const [env, expected] of [[{ VERCEL_ENV: "production", INGRESS_BRIDGE_TOKEN: "x" }, 1], [{ VERCEL_ENV: "preview", VERCEL_DEPLOYMENT_ID: "dpl_inline", INGRESS_BRIDGE_TOKEN: "x" }, 0]]) {
    const calls = [];
    const botFactory = (_config, enabled) => ({ call: async (method, params) => {
      if (!enabled) throw new Error("preview_network_prohibited");
      calls.push([method, params]);
      return true;
    } });
    const f = await fixture({ env, botFactory });
    const before = await f.state();
    const response = await invoke(telegram({ ...f.deps, fetcher, now: () => now }),
      { update_id: 77, inline_query: { id: "q", query: "джаз" } }, headers);
    assert.equal(response.statusCode, 200);
    assert.equal(calls.length, expected);
    if (expected) assert.equal(calls[0][1].results[0].id, concert);
    assert.deepEqual(await f.state(), before);
  }
});
