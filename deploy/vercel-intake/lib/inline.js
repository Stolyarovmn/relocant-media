// Inline-режим бота: «@бот запрос» в любом чате отдаёт карточки из публичной карты.
// Только чтение опубликованных JSON и answerInlineQuery: без состояния, квитанций и экспорта.

export const MAP_DATA = "https://stolyarovmn.github.io/relocant-media/map/";
export const BOT_APP = "https://t.me/relocant_media_belgrade_bot";
const ID = /^event_[a-f0-9]{20}$/u;
const zone = "Europe/Belgrade";
const when = new Intl.DateTimeFormat("ru", { timeZone: zone, day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
const kinds = { water: "💧 Вода", power: "⚡ Свет", road: "🚧 Дороги" };
const CACHE_MS = 60_000;
let cache = { at: 0, value: null };

const text = (value, limit) => (typeof value === "string" ? value.trim().slice(0, limit) : "");
const time = (value) => {
  const parsed = typeof value === "string" ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) ? parsed : null;
};
const fold = (value) => value.toLocaleLowerCase("ru").replace(/ё/gu, "е");

function interval(start, end) {
  if (start === null) return "";
  return end === null ? when.format(start) : `${when.format(start)} — ${when.format(end)}`;
}

/** Нормализует публичные выгрузки в общий вид карточек. Реклама и архив не попадают. */
export function inlineItems(incidents, events, now) {
  const works = (Array.isArray(incidents?.incidents) ? incidents.incidents : []).flatMap((item) => {
    const title = text(item?.title, 200);
    const end = time(item?.ends_at);
    if (!ID.test(item?.id ?? "") || !title || !kinds[item.kind] || item.archived === true
        || (end !== null && end <= now)) return [];
    return [{ id: item.id, places: false, title, label: kinds[item.kind],
      start: time(item.starts_at), end, place: text(item.reported_scope, 200) }];
  });
  const places = (Array.isArray(events?.events) ? events.events : []).flatMap((item) => {
    const title = text(item?.title, 200);
    const end = time(item?.ends_at) ?? time(item?.expires_at);
    if (!ID.test(item?.id ?? "") || !title || item.kind !== "event" || item.sponsored === true
        || item.archived === true || (end !== null && end <= now)) return [];
    return [{ id: item.id, places: true, title, label: "🎟 Афиша",
      start: time(item.starts_at), end: time(item.ends_at), place: text(item.venue?.name, 120) }];
  });
  return [...works, ...places];
}

/** Подбирает до 10 карточек: точный ID из миниапп или поиск по словам заголовка и места. */
export function inlineMatches(items, query) {
  const raw = text(query, 120);
  const id = raw.replace(/^places_/u, "");
  if (ID.test(id)) return items.filter((item) => item.id === id).slice(0, 1);
  const words = fold(raw).split(/\s+/u).filter(Boolean);
  const hits = items.filter((item) => {
    const haystack = fold(`${item.title} ${item.place} ${item.label}`);
    return words.every((word) => haystack.includes(word));
  });
  return hits.sort((a, b) => (a.start ?? 0) - (b.start ?? 0)).slice(0, 10);
}

export function inlineResult(item) {
  const link = `${BOT_APP}?startapp=${item.places ? "places_" : ""}${item.id}`;
  const period = interval(item.start, item.end);
  const description = [period, item.place].filter(Boolean).join(" · ");
  const lines = [`<b>${escape(item.title)}</b>`, escape(item.label)];
  if (period) lines.push(escape(period));
  if (item.place) lines.push(escape(item.place));
  return {
    type: "article", id: item.id, title: item.title, description: `${item.label}${description ? ` · ${description}` : ""}`,
    input_message_content: { message_text: lines.join("\n"), parse_mode: "HTML", link_preview_options: { is_disabled: true } },
    reply_markup: { inline_keyboard: [[{ text: "Открыть на карте", url: link }]] },
  };
}

function escape(value) {
  return value.replace(/[&<>]/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[char]);
}

async function readJSON(fetcher, name) {
  const response = await fetcher(`${MAP_DATA}${name}`, { signal: AbortSignal.timeout(3_000), redirect: "error" });
  if (!response.ok) throw new Error("map_data_unavailable");
  return response.json();
}

export async function mapData(fetcher = fetch, now = Date.now()) {
  if (cache.value && now - cache.at < CACHE_MS) return cache.value;
  const [incidents, events] = await Promise.all([
    readJSON(fetcher, "incidents.json").catch(() => null),
    readJSON(fetcher, "events.json").catch(() => null),
  ]);
  const value = { incidents, events };
  if (incidents || events) cache = { at: now, value };
  return value;
}

export function resetInlineCache() { cache = { at: 0, value: null }; }

/** Ответ на inline_query. Ошибка Telegram (устаревший запрос) не превращается в повтор webhook. */
export async function answerInline(update, bot, { fetcher = fetch, now = Date.now() } = {}) {
  const query = update.inline_query;
  if (typeof query?.id !== "string") return { status: "ignored" };
  const { incidents, events } = await mapData(fetcher, now);
  const results = inlineMatches(inlineItems(incidents, events, now), query.query).map(inlineResult);
  try {
    await bot.call("answerInlineQuery", {
      inline_query_id: query.id, results, cache_time: 60, is_personal: false,
      ...(results.length ? {} : { button: { text: "Открыть карту Белграда", start_parameter: "map" } }),
    });
    return { status: "ready", results: results.length };
  } catch {
    return { status: "inline_failed" };
  }
}
