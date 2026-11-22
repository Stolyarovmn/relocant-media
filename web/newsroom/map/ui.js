// Значки, категории афиши, тема и полоса дней. Модуль без DOM, чтобы проверять его в node --test.

const zone = "Europe/Belgrade";
const dayKeyFormat = new Intl.DateTimeFormat("en-CA", { timeZone: zone });
const hourFormat = new Intl.DateTimeFormat("en-GB", { timeZone: zone, hour: "2-digit", hourCycle: "h23" });
const weekdayFormat = new Intl.DateTimeFormat("ru", { timeZone: zone, weekday: "short" });
const dateFormat = new Intl.DateTimeFormat("ru", { timeZone: zone, day: "numeric" });
const monthFormat = new Intl.DateTimeFormat("ru", { timeZone: zone, day: "numeric", month: "long" });
const DAY = 86_400_000;

// Пути значков в квадрате 24×24 с центром в (12, 12). stroke — значок рисуется линией.
export const glyphs = {
  water: { d: "M12 4.6c3.2 4.4 5.2 7.4 5.2 9.6a5.2 5.2 0 0 1-10.4 0c0-2.2 2-5.2 5.2-9.6Z" },
  power: { d: "M13.4 4.4 7.4 13h4l-1.1 6.6 6.2-8.8h-4.1Z" },
  road: { d: "M6 10.3h12v3.4H6Z" },
  concert: { d: "M11.2 5.2 17.6 6.7v3.2l-4.1-1v6.6a3 2.6 0 1 1-2.3-2.5Z" },
  exhibition: { d: "M5.6 6.6h12.8v10.8H5.6Zm1.8 1.8v7.2h9.2V8.4Zm.8 6.4 2.7-3.6 2.1 2.4 1.3-1.3 1.5 2.5Z", rule: "evenodd" },
  sport: { d: "M8.6 4.8h2.5l.9 3.6.9-3.6h2.5l-2 5a4.4 4.4 0 1 1-2.8 0Z" },
  festival: { d: "M12 4.4l1.9 5.7 5.7 1.9-5.7 1.9-1.9 5.7-1.9-5.7-5.7-1.9 5.7-1.9Z" },
  event: { d: "M12 5.2l2 4.3 4.7.5-3.5 3.2 1 4.6L12 15.4l-4.2 2.4 1-4.6-3.5-3.2 4.7-.5Z" },
};

export const categories = {
  concert: "Концерт",
  exhibition: "Выставка",
  sport: "Спорт",
  festival: "Фестиваль",
  event: "Событие",
};

const categoryWords = [
  ["festival", /фестивал|festival|fest\b|чукафест/i],
  ["sport", /марафон|забег|полумарафон|матч|турнир|спорт|велогонк|maraton|trka/i],
  ["exhibition", /выставк|экспозиц|галере|музе|izložb|izlozb|exhibit/i],
  ["concert", /концерт|кабаре|фламенко|джаз|оркестр|koncert|concert/i],
];

/** Категория события по заголовку. Без совпадения — общее «Событие». */
export function eventCategory(title) {
  const text = String(title ?? "");
  return categoryWords.find(([, pattern]) => pattern.test(text))?.[0] ?? "event";
}

const escapeAttr = (value) => String(value).replace(/[&<>"]/g, (char) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[char]));

/**
 * SVG-значок в кружке. Цвет кружка задаёт CSS через data-kind/data-category,
 * заливка показывает время: filled — действует, outline — запланировано.
 */
export function markerSvg(type, { outline = false, count = 0, label = "" } = {}) {
  const glyph = glyphs[type] ?? glyphs.event;
  const rule = glyph.rule ? ` fill-rule="${glyph.rule}"` : "";
  const badge = count > 1
    ? `<g class="mk-count"><circle cx="20" cy="4.6" r="4.6"/><text x="20" y="4.6">${count > 99 ? "99+" : count}</text></g>`
    : "";
  const title = label ? `<title>${escapeAttr(label)}</title>` : "";
  return `<svg class="mk${outline ? " mk-outline" : ""}" viewBox="-1 -2 26 27" aria-hidden="${label ? "false" : "true"}"`
    + `${label ? ' role="img"' : ""}>${title}<circle class="mk-bub" cx="12" cy="12" r="11"/>`
    + `<path class="mk-glyph" d="${glyph.d}"${rule}/>${badge}</svg>`;
}

export function belgradeHour(now) {
  return Number(hourFormat.format(new Date(now)));
}

/** Авто-тема: светлая с 07:00 до 19:00 по Белграду, тёмная в остальное время. */
export function autoTheme(now) {
  const hour = belgradeHour(now);
  return hour >= 7 && hour < 19 ? "light" : "dark";
}

export function resolveTheme(choice, now) {
  return choice === "light" || choice === "dark" ? choice : autoTheme(now);
}

/** Следующий момент, когда авто-тема сменится: ближайшие 07:00 или 19:00. */
export function nextThemeSwitch(now) {
  for (let step = 1; step <= 24 * 60; step += 1) {
    const time = now + step * 60_000;
    if (autoTheme(time) !== autoTheme(now)) return time - (time % 60_000);
  }
  return now + DAY;
}

export function dayKey(time) {
  return dayKeyFormat.format(new Date(time));
}

/** Событие идёт или начинается в этот день по Белграду. Без конца — только день начала. */
export function placeOnDay(place, key) {
  if (!Number.isFinite(place?.start)) return false;
  const first = dayKey(place.start);
  const last = Number.isFinite(place.end) ? dayKey(place.end) : first;
  return first <= key && key <= last;
}

/** Полоса дней афиши: сегодня и следующие дни с категориями событий. */
export function dayStrip(places, now, count = 14) {
  return Array.from({ length: count }, (_, index) => {
    const time = now + index * DAY;
    const key = dayKey(time);
    const items = places.filter((place) => !place.paidPlace && placeOnDay(place, key));
    const kinds = [...new Set(items.map((place) => eventCategory(place.title)))];
    const weekday = weekdayFormat.format(new Date(time));
    return {
      key,
      weekday,
      day: dateFormat.format(new Date(time)),
      label: `${index === 0 ? "Сегодня, " : ""}${monthFormat.format(new Date(time))}`,
      weekend: weekday === "сб" || weekday === "вс",
      count: items.length,
      categories: kinds.slice(0, 4),
    };
  });
}
