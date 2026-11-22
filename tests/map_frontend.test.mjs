import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import { buildCalendar, googleCalendarUrl } from "../web/newsroom/map/calendar.js";
import {
  addressHits, addressWindowText, agendaDays, belgradeDate, buildAddressIndex, clusterMapPoints, exactMapPoints, incidentCountdown,
  incidentInPeriod, incidentStatus, incidentTimes, launchDigestEvents, launchEvent, launchMap, launchPlaces,
  mapTheme, nextTransition, placeCategory, placeCountdown, placeOnDay, placeOptionLabel, placeStatus,
  pointsAtScale, readIncidents, readPlaces, searchAddresses, shareLink, straightLineDistance, streetBounds,
  unexpired, visibleMapLines,
} from "../web/newsroom/map/core.js";
import { agendaVisit, setupPlaces } from "../web/newsroom/map/places.js";
import { agendaFavorites } from "../web/newsroom/map/favorites.js";
import {
  corridorCaps, heatCells, heatOverview, layerView, timelineBands, timelineInstant,
  timelineLabel, timelineSteps,
} from "../web/newsroom/map/layer-views.js";
import { loadSnapshot } from "../web/newsroom/map/snapshots.js";

test("тепловой обзор сохраняет точную геометрию на крупном масштабе и при точном поиске", () => {
  assert.equal(layerView("unrecognized"), "detail");
  assert.equal(layerView("corridor"), "corridor");
  assert.equal(layerView("heat"), "heat");
  assert.equal(heatOverview("heat", 14, false), true);
  assert.equal(heatOverview("heat", 15, false), false);
  assert.equal(heatOverview("heat", 12, true), false);
  assert.equal(heatOverview("detail", 12, false), false);
});

test("плотность считает объявления в ячейке и пропускает приблизительные ориентиры", () => {
  const water = { id: "water", kind: "water" };
  const power = { id: "power", kind: "power" };
  const road = { id: "road", kind: "road" };
  const project = ([lat, lon]) => ({ x: lon, y: lat });
  const point = (lat, lon, incident, approximate = false) => ({ lat, lon, incident, approximate });
  const result = heatCells([
    point(22, 15, water), point(20, 30, water), point(22, 21, power), point(32, 19, road, true),
    point(20, 80, water),
  ], project, 64);
  assert.deepEqual(result, [
    { x: 32, y: 32, incidents: [water, power] },
    { x: 96, y: 32, incidents: [water] },
  ]);
});

test("концы участков берутся с исходной линии и скрываются, если знаки наложатся", () => {
  const line = [[1, 1], [8, 20], [2, 24]];
  const project = ([lat, lon]) => ({ x: lon, y: lat });
  assert.deepEqual(corridorCaps(line, project), [[1, 1], [2, 24]]);
  assert.deepEqual(corridorCaps(line, ([lat, lon]) => ({ x: lon / 2, y: lat / 2 })), []);
});

test("временной вид фиксирует ближайшие полчаса и подписывает время Белграда", () => {
  const origin = Date.parse("2026-11-23T22:48:00Z");
  assert.equal(layerView("timeline"), "timeline");
  assert.equal(timelineInstant(origin, 0), origin);
  assert.equal(timelineInstant(origin, 1), Date.parse("2026-11-23T23:00:00Z"));
  assert.equal(timelineInstant(origin, 2), Date.parse("2026-11-23T23:30:00Z"));
  assert.ok(timelineInstant(origin, timelineSteps) - origin <= 24 * 3_600_000);
  assert.equal(timelineLabel(origin, true), "Сейчас");
  assert.match(timelineLabel(timelineInstant(origin, 2), false), /24.*00:30/u);
});

test("полосы работ сохраняют паузы и объединяют пересекающиеся интервалы одного слоя", () => {
  const incident = (kind, spans, archived = false) => ({
    kind, archived, windows: spans.map(([start, end]) => ({ start, end })),
  });
  const bands = timelineBands([
    incident("water", [[1, 4], [3, 6], [8, 12]]),
    incident("power", [[5, 10]]),
    incident("road", [[1, 9]], true),
    incident("water", [[0, 1], [10, 14], [5, null]]),
  ], 2, 10);
  assert.deepEqual([...bands], [["water", [[2, 6], [8, 10]]], ["power", [[5, 10]]]]);
});

test("выбранное время оставляет на карте только действующую зону одного объявления", () => {
  const origin = Date.parse("2026-11-24T14:08:00Z");
  const first = { start: Date.parse("2026-11-24T07:00:00Z"),
    end: Date.parse("2026-11-24T14:00:00Z") };
  const second = { start: first.start, end: Date.parse("2026-11-24T15:00:00Z") };
  const notice = { start: first.start, end: second.end, windows: [first, second],
    archived: false, points: [], lines: [], areas: [], addresses: [], ranges: [], segments: [],
    settlements: [], waterDistricts: [
      { municipality: "Савски-Венац", window: first },
      { municipality: "Вождовац", window: second },
    ] };
  const picked = incidentInPeriod(notice, timelineInstant(origin, 1), 0);
  assert.deepEqual(picked.waterDistricts.map(({ municipality }) => municipality), ["Вождовац"]);
  assert.equal(incidentInPeriod(notice, timelineInstant(origin, 2), 0), null);
});

test("новые события отсчитываются от последней просмотренной версии афиши", () => {
  const old = "2026-11-22T08:00:00Z";
  const current = "2026-11-22T09:00:00Z";
  const post = Date.parse("2026-11-22T08:30:00Z");
  let saved = null;
  const storage = () => ({ getItem: () => saved, setItem: (_key, value) => { saved = value; } });
  const first = agendaVisit(storage);
  first.observe(old);
  assert.equal(first.hasNew(post), false);

  const next = agendaVisit(storage);
  next.observe(current);
  assert.equal(next.hasNew(post), true);
  assert.equal(next.hasNew(Date.parse(old)), false);
  assert.equal(next.hasNew(Date.parse("2026-11-22T10:00:00Z")), false);
  assert.equal(next.hasNew(null), false);
  next.observe(old); // При сбое сети старый снимок не меняет отсчёт будущих посещений.
  assert.equal(next.hasNew(post), false);
  assert.equal(saved, current);

  const again = agendaVisit(storage);
  again.observe(current);
  assert.equal(again.hasNew(post), false);
});

test("афиша остаётся доступной при запрете локального хранилища", () => {
  const visit = agendaVisit(() => { throw new Error("Браузерное хранилище выключено"); });
  visit.observe("2026-11-22T09:00:00Z");
  assert.equal(visit.hasNew(Date.parse("2026-11-22T08:30:00Z")), false);
});

function favoritesTelegram(keys) {
  return {
    initData: "preview", isVersionAtLeast: () => true,
    CloudStorage: {
      getKeys: (done) => done(null, [...keys.keys()]),
      setItem(key, value, done) { keys.set(key, value); done(null, true); },
      removeItem: (key, done) => done(null, keys.delete(key)),
    },
  };
}

test("избранное в Telegram хранит отдельные ключи событий и подтверждает запись", async () => {
  const keys = new Map([["another_preference", "1"]]);
  const telegram = favoritesTelegram(keys);
  const blockedLocal = () => { throw new Error("Браузерное хранилище выключено"); };
  const firstSession = agendaFavorites(telegram, blockedLocal);
  await firstSession.write("event_11111111111111111111", true);
  const secondSession = agendaFavorites(telegram, blockedLocal);
  await secondSession.write("event_22222222222222222222", true);
  await firstSession.write("event_11111111111111111111", false);
  assert.deepEqual([...await secondSession.read()], ["event_22222222222222222222"]);
  assert.equal(keys.get("another_preference"), "1");
  telegram.CloudStorage.setItem = (_key, _value, done) => done(null, false);
  await assert.rejects(firstSession.write("event_11111111111111111111", true));
});

test("сайт вне Telegram сохраняет избранное в этом браузере", async () => {
  const keys = new Map([["theme", "dark"]]);
  const storage = {
    get length() { return keys.size; },
    key: (index) => [...keys.keys()][index],
    setItem: (key, value) => keys.set(key, value),
    removeItem: (key) => keys.delete(key),
  };
  const getStorage = () => storage;
  const id = "event_11111111111111111111";
  await agendaFavorites(undefined, getStorage).write(id, true);
  assert.deepEqual([...await agendaFavorites(undefined, getStorage).read()], [id]);
  await agendaFavorites(undefined, getStorage).write(id, false);
  assert.deepEqual([...keys.keys()], ["theme"]);
});

test("the automatic theme changes at seven and nineteen in Belgrade", () => {
  assert.equal(mapTheme("auto", Date.parse("2026-11-22T05:59:00Z")), "dark");
  assert.equal(mapTheme("auto", Date.parse("2026-11-22T06:00:00Z")), "light");
  assert.equal(mapTheme("auto", Date.parse("2026-07-22T17:00:00Z")), "dark");
  assert.equal(mapTheme("light", Date.parse("2026-11-22T01:00:00Z")), "light");
  assert.equal(mapTheme("dark", Date.parse("2026-11-22T12:00:00Z")), "dark");
});

test("agenda dates follow Belgrade midnight and include multiday events", () => {
  const start = Date.parse("2026-10-24T22:30:00Z");
  const end = Date.parse("2026-10-27T00:00:00+01:00");
  assert.equal(belgradeDate(start), "2026-10-25");
  assert.deepEqual(agendaDays(start, 3), ["2026-10-25", "2026-10-26", "2026-10-27"]);
  assert.equal(placeOnDay({ start, end }, "2026-10-25"), true);
  assert.equal(placeOnDay({ start, end }, "2026-10-26"), true);
  assert.equal(placeOnDay({ start, end }, "2026-10-27"), false);
  assert.equal(placeOnDay({ start, end: null }, "2026-10-25"), true);
  assert.equal(placeOnDay({ start, end: null }, "2026-10-26"), false);
  assert.equal(placeCategory({ title: "Koncert u Beogradu" }), "music");
  assert.equal(placeCategory({ title: "Muzičko veče u Beogradu" }), "music");
  assert.equal(placeCategory({ title: "Выставка в спортивном центре" }), "exhibition");
  assert.equal(placeCategory({ title: "Beogradski polumaraton" }), "sport");
});

test("Google Calendar uses a verified end time and the Belgrade time zone", () => {
  const item = { title: "Концерт & джаз", venue: "Дом культуры", address: "Beograd",
    source: "https://example.rs/event", start: Date.parse("2026-12-01T19:30:00+01:00"),
    end: Date.parse("2026-12-01T21:00:00+01:00") };
  const now = Date.parse("2026-11-30T10:00:00Z");
  const url = new URL(googleCalendarUrl(item, now));
  assert.equal(url.origin, "https://calendar.google.com");
  assert.equal(url.searchParams.get("text"), item.title);
  assert.equal(url.searchParams.get("dates"), "20261201T183000Z/20261201T200000Z");
  assert.equal(url.searchParams.get("stz"), "Europe/Belgrade");
  assert.equal(url.searchParams.get("location"), "Дом культуры, Beograd");
  assert.equal(googleCalendarUrl({ ...item, end: null }, now), "");
  assert.equal(googleCalendarUrl({ ...item, scheduleNote: "Время уточняется" }, now), "");
});

const waterId = "event_0123456789abcdef1234";
const roadId = "event_0123456789abcdef1235";
const powerId = "event_0123456789abcdef1236";

const addressIndex = buildAddressIndex({
  version: 1,
  addresses: [
    ["Vojvode Šupljikca", "42", "Vračar", 44.79823, 20.48625],
    ["Vojvode Šupljikca", "42A", "Vračar", 44.79837, 20.48627],
    ["Vojvode Šupljikca", "42", "Zvezdara", 44.801, 20.491],
    ["Kralja Bodina", "3", "Voždovac", 44.79012, 20.48313],
    ["Kralja Bodina", "9", "Voždovac", 44.79113, 20.48419],
  ],
});

function incident(record = {}) {
  return readIncidents({ version: 1, incidents: [{
    id: waterId, kind: "water", title: "Работы на водопроводе",
    source_url: "https://www.beograd.rs/vesti/", ...record,
  }] })[0];
}

test("карта возвращает проверенный снимок после ошибки сети и повреждённого файла", async () => {
  const responses = new Map();
  const cache = {
    match: async (url) => responses.get(url)?.clone(),
    put: async (url, response) => responses.set(url, response.clone()),
  };
  const storage = { open: async () => cache };
  const url = "./incidents.json";
  const generated_at = "2026-11-22T07:00:00Z";
  const data = { version: 1, generated_at, incidents: [] };
  const online = await loadSnapshot(url, readIncidents, { storage, fetcher: async () => Response.json(data) });
  assert.equal(online.cached, false);
  assert.equal(online.generatedAt, generated_at);
  for (const fetcher of [
    async () => { throw new TypeError("network"); },
    async () => Response.json({ version: 1, events: [] }),
    async () => Response.json({ ...data, generated_at: "2026-11-21T07:00:00Z" }),
  ]) {
    const saved = await loadSnapshot(url, readIncidents, { storage, fetcher });
    assert.equal(saved.cached, true);
    assert.equal(saved.generatedAt, generated_at);
  }
  const recovered = await loadSnapshot(url, readIncidents, {
    storage, fetcher: async () => Response.json({ ...data, generated_at: "2026-11-22T08:00:00Z" }),
  });
  assert.equal(recovered.cached, false);
  assert.equal(recovered.generatedAt, "2026-11-22T08:00:00Z");
  assert.equal(responses.size, 1);
});

test("кэш выключен в некоторых браузерах, свежие данные всё равно открываются", async () => {
  const storage = { open: async () => { throw new Error("storage blocked"); } };
  const readable = await loadSnapshot("./events.json", readPlaces, {
    storage, fetcher: async () => Response.json({ version: 1, events: [] }),
  });
  assert.deepEqual(readable.entries, []);
  assert.equal(readable.cached, false);
  await assert.rejects(loadSnapshot("./events.json", readPlaces, {
    storage, fetcher: async () => new Response(null, { status: 503 }),
  }));
});

test("офлайн-оболочка содержит текущие версии модулей и стилей карты", async () => {
  const fromMap = (file) => readFile(new URL(`../web/newsroom/map/${file}`, import.meta.url), "utf8");
  const [worker, ...sources] = await Promise.all(
    ["offline-worker.js", "index.html", "app.js", "places.js"].map(fromMap),
  );
  for (const source of sources) {
    for (const [asset] of source.matchAll(/[\w-]+\.(?:js|css)\?v=\d+/g)) {
      assert.ok(worker.includes(`"./${asset}"`), asset);
    }
  }
  assert.ok(!worker.includes("tile.openstreetmap.org"));
  assert.ok(!worker.includes("telegram.org"));
});

test("фильтр текущих работ оставляет только район со своим активным графиком", () => {
  const first = { starts_at: "2026-11-24T08:00:00+01:00", ends_at: "2026-11-24T15:00:00+01:00" };
  const second = { starts_at: first.starts_at, ends_at: "2026-11-24T16:00:00+01:00" };
  const road = (window, label) => ({ ...window, label, coordinates: [[44.8, 20.4], [44.81, 20.4]] });
  const item = incident({
    starts_at: first.starts_at, ends_at: second.ends_at, windows: [first, second],
    water_districts: [{ ...first, municipality: "Savski venac" }, { ...second, municipality: "Voždovac" }],
    addresses: [{ ...first, street: "Fabrisova", number: "1" }, { ...second, street: "Brane Ćosića", number: "16" }],
    lines: [road(first, "Fabrisova"), road(second, "Brane Ćosića")],
  });
  const now = Date.parse(first.ends_at);
  const current = incidentInPeriod(item, now, 0);
  assert.deepEqual(current.waterDistricts.map((district) => district.municipality), ["Voždovac"]);
  assert.deepEqual(current.lines.map((line) => line.label), ["Brane Ćosića"]);
  assert.deepEqual(current.addresses.map((address) => address.number), ["16"]);
  assert.equal(incidentInPeriod(item, now).lines.length, 2);
});

test("фильтр периода учитывает будущие окна, точную границу срока и неизвестное время", () => {
  const planned = incident({ starts_at: "2026-11-24T08:00:00+01:00", ends_at: "2026-11-24T16:00:00+01:00" });
  const now = Date.parse("2026-11-23T07:00:00+01:00");
  assert.equal(incidentInPeriod(planned, now, 0), null);
  assert.equal(incidentInPeriod(planned, now, 24), null);
  assert.ok(incidentInPeriod(planned, now, 168));
  const boundary = nextTransition([planned], now, 24);
  assert.equal(boundary, Date.parse("2026-11-23T08:00:00+01:00"));
  assert.ok(incidentInPeriod(planned, boundary, 24));
  assert.equal(incidentInPeriod(planned, Date.parse("2026-11-24T16:00:00+01:00"), 168), null);
  const unknown = incident();
  assert.equal(incidentInPeriod(unknown, now, 168), null);
  assert.equal(incidentInPeriod(unknown, now), unknown);
});

test("открытие объявления по ссылке браузера и Telegram", () => {
  assert.equal(launchEvent(`?event=${waterId}`, roadId), waterId);
  assert.equal(launchEvent(`?tgWebAppStartParam=${roadId}`, waterId), roadId);
  assert.equal(launchEvent("?event=unknown", powerId), powerId);
  assert.equal(launchEvent("?tgWebAppStartParam=javascript%3Aalert%281%29", ""), "");
});

test("ссылка сводки открывает только перечисленные объявления", () => {
  assert.deepEqual(launchDigestEvents(`?events=${roadId}~${waterId}`), [roadId, waterId]);
  assert.deepEqual(launchDigestEvents("", `digest_${roadId}-${powerId}`), [roadId, powerId]);
  assert.deepEqual(launchDigestEvents("", `digest_${roadId}-invalid`), []);
});

test("перекрытие с датами из выгрузки даёт календарное событие на весь день", () => {
  const road = incident({
    id: roadId, kind: "road", starts_on: "2026-11-14", ends_on: "2026-12-31",
    starts_at: "2026-11-14T00:00:00+01:00", ends_at: "2027-01-01T00:00:00+01:00",
  });
  const calendar = buildCalendar(road, Date.parse("2026-11-14T12:00:00+01:00"));
  assert.match(calendar, /DTSTART;VALUE=DATE:20261114\r\nDTEND;VALUE=DATE:20270101/);
  assert.equal(buildCalendar(road, Date.parse("2027-01-01T00:00:00+01:00")), null);
});

test("ссылка на афишу отделена от существующих ссылок городских работ", () => {
  const places = { mode: "places", id: waterId };
  assert.deepEqual(launchMap(`?mode=places&event=${waterId}`), places);
  assert.deepEqual(launchMap(`?tgWebAppStartParam=places_${waterId}`), places);
  assert.deepEqual(launchMap("", `places_${waterId}`), places);
  assert.deepEqual(launchMap("", "places"), { mode: "places", id: "" });
  assert.deepEqual(launchMap(`?event=${waterId}`, "places"), { mode: "incidents", id: waterId });
  assert.deepEqual(launchMap("", waterId), { mode: "incidents", id: waterId });
  assert.deepEqual(launchMap("", "places_bad"), { mode: "incidents", id: "" });
});

test("отсчёт афиши использует только подтверждённое время, технический срок скрыт", () => {
  const sample = { id: waterId, kind: "event", title: "Концерт", timezone: "Europe/Belgrade",
    source_url: "https://www.beograd.rs/", starts_at: "2026-11-13T19:00:00+01:00",
    ends_at: "2026-11-13T21:00:00+01:00", expires_at: "2026-11-13T21:00:00+01:00",
    venue: { name: "Площадка", address: "Теразие", latitude: 44.816, longitude: 20.459 } };
  const schedule = "https://www.beograd.rs/koncert";
  const [known, unknown] = readPlaces({ version: 1, events: [
    { ...sample, schedule_source_url: schedule },
    { ...sample, id: roadId, ends_at: null, expires_at: "2026-11-14T00:00:00+01:00" },
    { ...sample, id: powerId, ends_at: "неизвестно" },
    { ...sample, id: powerId, venue: { ...sample.venue, latitude: 99 } },
  ] });
  assert.equal(known.schedule, schedule);
  assert.equal(unknown.schedule, "");
  for (const url of [sample.source_url, "javascript:alert(1)"]) {
    assert.equal(readPlaces({ version: 1, events: [{ ...sample, schedule_source_url: url }] })[0].schedule, "");
  }
  const now = (time) => Date.parse(`2026-11-13T${time}+01:00`);
  assert.equal(placeCountdown(known, now("18:00")), "До начала · 1 ч");
  assert.equal(placeCountdown(known, now("20:00")), "До окончания · 1 ч");
  assert.equal(placeCountdown(known, now("21:00")), "Завершилось");
  assert.equal(placeStatus(unknown, now("23:59")), "active");
  assert.equal(placeCountdown(unknown, now("23:59")), "Началось");
  assert.equal(placeCountdown(unknown, unknown.expires), "Прошедшая дата");
  assert.equal(placeStatus({ ...known, archived: true }, now("20:00")), "closed");
  const [uncertain] = readPlaces({ version: 1, events: [
    { ...sample, schedule_note: "В СМИ 24 ноября, в билетах 25 ноября. Уточните дату." },
  ] });
  assert.match(uncertain.scheduleNote, /Уточните дату/);
  assert.equal(placeCountdown(uncertain, now("18:00")), "Дата уточняется");
  assert.equal(buildCalendar(uncertain, now("18:00")), null);
});

const race = {
  id: roadId, kind: "event", title: "Белградский полумарафон", timezone: "Europe/Belgrade",
  source_url: "https://www.beograd.rs/", starts_at: "2090-11-24T09:00:00+01:00",
  ends_at: "2090-11-24T13:00:00+01:00", expires_at: "2090-11-24T13:00:00+01:00",
  venue: { name: "Старт", address: "Теразие", latitude: 44.816, longitude: 20.459 },
  courses: [
    { name: "21,1 км", coordinates: [[20.459, 44.816], [20.430, 44.823], [20.459, 44.816]] },
    { name: "10 км", coordinates: [[20.459, 44.816], [20.440, 44.800]] },
  ],
};

test("афиша нормализует обе трассы и сохраняет старые карточки с маркером", () => {
  const readCourses = (courses) => readPlaces({ version: 1, events: [{ ...race, courses }] })[0].courses;
  assert.deepEqual(readCourses(race.courses), [
    { name: "21,1 км", latLngs: [[44.816, 20.459], [44.823, 20.430], [44.816, 20.459]] },
    { name: "10 км", latLngs: [[44.816, 20.459], [44.800, 20.440]] },
  ]);
  const [course] = race.courses;
  for (const invalid of [undefined, Array(5).fill(course), [...race.courses, course],
    [{ ...course, coordinates: [[20.459, 44.816]] }],
    [{ ...course, coordinates: [[20.459, 44.816], [20.459, 44.816]] }],
    [{ ...course, coordinates: [[20.459, 44.816], [20.430, Infinity]] }],
    [{ ...course, coordinates: [[20.459, 44.816], ["20.430", 44.823]] }],
    [{ ...course, coordinates: [[20.459, 44.816], [44.823, 20.430]] }],
    [{ ...course, coordinates: Array(4097).fill([20.459, 44.816]) }],
  ]) assert.deepEqual(readCourses(invalid), []);
});

test("ссылка на событие показывает трассы, а избранное фильтрует карту и список", async (t) => {
  function make(tag, className, textContent) {
    return {
      tag, className, textContent, children: [], style: {}, dataset: {}, events: {}, attributes: {},
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      addEventListener(name, fn) { this.events[name] = fn; },
      setAttribute(name, value) { this.attributes[name] = value; },
      querySelector(tagName) { return this.children.find((child) => child.tag === tagName); },
      focus() {},
    };
  }
  const groups = [];
  const leaflet = {
    layerGroup() {
      return {
        layers: [],
        addTo() { groups.push(this); return this; },
        clearLayers() { this.layers = []; },
      };
    },
    latLngBounds(points) { return points; },
    divIcon(options) { return options; },
    polyline(points, options) {
      return { addTo(group) { group.layers.push({ points, options }); } };
    },
    marker(point) {
      return { on() {}, addTo(group) { group.layers.push(point); } };
    },
  };
  const map = {
    on() {}, closePopup() {}, invalidateSize() {},
    getZoom() { return 12; },
    project([lat, lon]) { return { x: lat * 1200, y: lon * 1200 }; },
    fitBounds(points) { this.bounds = points; },
  };
  const savedLeaflet = globalThis.L;
  const savedDocument = globalThis.document;
  t.after(() => {
    globalThis.L = savedLeaflet;
    globalThis.document = savedDocument;
  });
  globalThis.L = leaflet;
  globalThis.document = {
    hidden: true, addEventListener() {},
    createElement: make, createTextNode: (text) => text,
  };
  const elements = new Map();
  const byId = (id) => {
    if (!elements.has(id)) elements.set(id, make("div", id));
    return elements.get(id);
  };
  const other = { ...race, id: "event_22222222222222222222", courses: [],
    venue: { ...race.venue, latitude: 44.75 } };
  const keys = new Map();
  const places = setupPlaces({
    map, mobile: { matches: true, addEventListener() {} }, animate: false, byId, make,
    telegram: favoritesTelegram(keys),
    link: (_url, title) => make("a", "", title),
    loadSnapshot: async (_path, decode) => ({ entries: decode({ version: 1, events: [race, other] }), cached: false }),
    onSync() {},
  });
  t.after(() => places.leave());
  places.enter(roadId);
  await new Promise(setImmediate);

  const [, courses] = groups;
  assert.equal(courses.layers.length, 4);
  const [first, second] = courses.layers.slice(2);
  assert.notEqual(first.options.color, second.options.color);
  assert.equal(first.options.dashArray, undefined);
  assert.ok(second.options.dashArray);
  assert.deepEqual(map.bounds, [[44.816, 20.459], ...first.points, ...second.points]);
  const detail = byId("place-detail");
  const legend = detail.querySelector("ul");
  assert.equal(legend.attributes["aria-label"], "Трассы на карте");
  assert.deepEqual(legend.children.map(({ children }) => children[1].textContent), ["21,1 км", "10 км"]);

  detail.querySelector("button").events.click();
  assert.equal(detail.hidden, true);
  assert.deepEqual(courses.layers, []);
  places.enter(roadId);
  assert.equal(courses.layers.length, 4);
  const favoriteButton = () => detail.children.find((child) => child.className === "place-favorite");
  assert.equal(favoriteButton().attributes["aria-pressed"], "false");
  favoriteButton().events.click();
  await new Promise(setImmediate);
  assert.equal(favoriteButton().attributes["aria-pressed"], "true");
  assert.equal(keys.size, 1);
  byId("toggle-favorites").events.click();
  await new Promise(setImmediate);
  assert.deepEqual(byId("places-list").children.map((child) => child.dataset.placeId), [roadId]);
  assert.equal(groups[0].layers.length, 1);

  byId("places-list").children[0].events.click();
  favoriteButton().events.click();
  await new Promise(setImmediate);
  assert.equal(keys.size, 0);
  assert.equal(detail.hidden, true);
  assert.deepEqual(byId("places-list").children, []);
  assert.deepEqual(groups[0].layers, []);
  places.enter(roadId);
  assert.equal(byId("toggle-favorites").attributes["aria-pressed"], "false");
  places.leave();
  assert.deepEqual(courses.layers, []);
});

test("расстояние по прямой считается по двум координатам", () => {
  const origin = { lat: 44.816, lon: 20.459 };
  assert.equal(straightLineDistance(origin, origin), 0);
  assert.ok(Math.abs(straightLineDistance(origin, { lat: 44.817, lon: 20.459 }) - 111.2) < 1);
  assert.ok(Math.abs(straightLineDistance(origin, { lat: 44.816, lon: 20.46 }) - 78.9) < 1);
});

test("поиск сербских улиц латиницей и кириллицей с точным номером дома", () => {
  const matching = searchAddresses(addressIndex, "Војводе Шупљикца 42", true);
  assert.equal(matching.length, 2);
  assert(matching.every((item) => item.number === "42"));
  const suffix = searchAddresses(addressIndex, "Vojvode Supljikca 42a", true);
  assert.equal(suffix.length, 1);
  assert.equal(suffix[0].number, "42A");
  assert.deepEqual(searchAddresses(addressIndex, "Vojvode Supljikca 4", true), []);
  assert.deepEqual(searchAddresses(addressIndex, "Vojvode Supljikca 42 Vracar", true)
    .map((item) => item.number), ["42"]);
  assert(searchAddresses(addressIndex, "Vojvode Supljikca 4").length > 0);
});

test("метка дома требует точного адреса, суффикса и общины", () => {
  const address = { street: "Војводе Шупљикца", number: "42", municipality: "Врачар" };
  const event = incident({ id: powerId, kind: "power", addresses: [
    { ...address, starts_at: "2026-11-13T09:00:00+01:00", ends_at: "2026-11-13T12:00:00+01:00" },
    { ...address, number: "4-2" },
    { ...address, number: "42B" },
    { ...address, municipality: "Врачар, друго насеље" },
    { ...address, municipality: "" },
  ] });
  const match = searchAddresses(addressIndex, "Vojvode Supljikca 42 Vračar", true)[0];
  const hits = addressHits(event, match);
  assert.equal(hits.length, 1);
  assert.match(addressWindowText(hits[0].window), /09:00–12:00/);
  assert.deepEqual(exactMapPoints(addressIndex, event).map(({ lat, lon }) => [lat, lon]), [
    [44.79823, 20.48625],
  ]);
  const duplicate = buildAddressIndex({ version: 1, addresses: [
    ["Vojvode Šupljikca", "42", "Vračar", 44.79823, 20.48625],
    ["Војводе Шупљикца", "42", "Врачар", 44.812, 20.512],
  ] });
  assert.deepEqual(exactMapPoints(duplicate, event), []);
});

test("улица из индекса даёт рамку просмотра, но не создаёт линию отключения", () => {
  const event = incident({ segments: [{
    street: "Краља Бодина", start: "Видска", end: "Крушевачка", municipality: "Вождовац",
  }] });
  assert.deepEqual(streetBounds(addressIndex, event), [44.79012, 20.48313, 44.79113, 20.48419]);
  assert.deepEqual(event.lines, []);
  const unmatched = incident({ segments: [{ street: "Краља Бодина", municipality: "Земун" }] });
  assert.equal(streetBounds(addressIndex, unmatched), null);
});

test("время показывается по Белграду, между окнами событие не действует", () => {
  const event = incident({
    starts_at: "2026-11-13T08:00:00+01:00",
    ends_at: "2026-11-13T17:00:00+01:00",
    windows: [
      { starts_at: "2026-11-13T08:00:00+01:00", ends_at: "2026-11-13T10:00:00+01:00" },
      { starts_at: "2026-11-13T15:00:00+01:00", ends_at: "2026-11-13T17:00:00+01:00" },
    ],
  });
  assert.equal(incidentStatus(event, Date.parse("2026-11-13T06:00:00Z")), "planned");
  assert.equal(incidentStatus(event, Date.parse("2026-11-13T08:30:00Z")), "active");
  assert.equal(incidentStatus(event, Date.parse("2026-11-13T11:00:00Z")), "between");
  assert.equal(incidentStatus(event, Date.parse("2026-11-13T16:00:00Z")), "closed");
  assert.match(incidentTimes(event)[0], /08:00–10:00/);
  assert.match(incidentTimes(event)[1], /15:00–17:00/);
});

test("для дорожного периода даты показываются без искусственных часов", () => {
  const event = incident({ id: roadId, kind: "road", starts_on: "2026-11-13", ends_on: "2026-11-15",
    starts_at: "2026-11-13T00:00:00+01:00", ends_at: "2026-11-16T00:00:00+01:00" });
  assert.equal(incidentStatus(event, Date.parse("2026-11-15T18:00:00Z")), "active");
  assert.equal(incidentStatus(event, Date.parse("2026-11-16T00:00:00Z")), "closed");
  assert.match(incidentTimes(event)[0], /13.*15/);
  assert(!incidentTimes(event)[0].includes("00:00"));
});

test("архивный снимок дороги скрыт из действующих даже внутри прежнего периода", () => {
  const event = incident({ id: roadId, kind: "road", archived: true,
    starts_at: "2026-11-13T00:00:00+01:00", ends_at: "2026-11-16T00:00:00+01:00" });
  assert.equal(event.archived, true);
  assert.equal(incidentStatus(event, Date.parse("2026-11-14T12:00:00Z")), "closed");
});

test("контур заменяет метку дома, а каждое окно заканчивается самостоятельно", () => {
  const address = { street: "Vojvode Šupljikca", number: "42", municipality: "Vračar",
    starts_at: "2026-11-13T08:00:00+01:00", ends_at: "2026-11-13T10:00:00+01:00" };
  const later = { ...address, number: "42A", ends_at: "2026-11-13T11:00:00+01:00" };
  const ring = [[44.7982, 20.48625], [44.7983, 20.48625], [44.7983, 20.48635],
    [44.7982, 20.48625]];
  const event = incident({ id: powerId, kind: "power", addresses: [address, later],
    areas: [{ scope: "building", label: "Дом 42", rings: [ring], address,
      starts_at: address.starts_at, ends_at: address.ends_at },
    { scope: "settlement", rings: [[...ring.slice(0, 3), [44.79, 20.48]]] }],
    windows: [address, later],
  });
  const now = Date.parse("2026-11-13T09:20:00+01:00");
  const boundary = Date.parse(address.ends_at);
  assert.equal(event.areas.length, 1);
  assert.equal(nextTransition([event], now), boundary);
  assert.match(incidentCountdown(event, now), /ближайшего окончания · 40 мин/);
  assert.equal(unexpired(event.areas[0].window, boundary), false);
  assert.equal(unexpired(event.areas[0].window, boundary, true), true);
  assert.deepEqual(exactMapPoints(addressIndex, event, now).map(p => p.address.number), ["42A"]);
  assert.deepEqual(exactMapPoints(addressIndex, event, boundary).map(p => p.address.number), ["42A"]);
  assert.match(incidentCountdown(event, boundary), /До окончания · 1 ч/);
  assert.equal(incidentCountdown(event, Date.parse(later.ends_at)), "");
});

test("одиночная дорожная точка из старой выгрузки остаётся ориентиром без границ", () => {
  const road = incident({ id: roadId, kind: "road", points: [{ lat: 44.81, lon: 20.44 }] });
  assert.deepEqual(exactMapPoints(addressIndex, road).map(({ lat, lon }) => [lat, lon]), [[44.81, 20.44]]);
  assert.deepEqual(road.lines, []);
  assert.deepEqual(road.areas, []);
});

test("водный участок с собственным окном исчезает отдельно от остального объявления", () => {
  const geometry = { coordinates: [[44.81, 20.4], [44.81, 20.41]] };
  const event = incident({
    starts_at: "2026-11-13T08:00:00+01:00", ends_at: "2026-11-13T15:00:00+01:00",
    lines: [
      { ...geometry, label: "Ранний участок", starts_at: "2026-11-13T08:00:00+01:00",
        ends_at: "2026-11-13T11:00:00+01:00" },
      { ...geometry, label: "Участок без отдельного окна" },
    ],
  });
  const end = Date.parse("2026-11-13T11:00:00+01:00");
  assert.equal(nextTransition([event], end - 1), end);
  assert.match(addressWindowText(event.lines[0].window), /08:00–11:00/);
  assert.deepEqual(visibleMapLines(event, end - 1), event.lines);
  assert.deepEqual(visibleMapLines(event, end), event.lines.slice(1));
  assert.deepEqual(visibleMapLines(event, event.end), []);
  assert.deepEqual(visibleMapLines(event, event.end, true), event.lines);
});

test("точки разных слоёв группируются через границу ячеек и разделяются при приближении", () => {
  const points = [
    { lat: 12, lon: 42, kind: "water" },
    { lat: 12, lon: 70, kind: "power" },
    { lat: 12, lon: 170, kind: "road" },
  ];
  const project = (scale) => ([lat, lon]) => ({ x: lon * scale, y: lat * scale });
  assert.deepEqual(clusterMapPoints(points, project(1), 43), [points.slice(0, 2), points.slice(2)]);
  assert.deepEqual(clusterMapPoints(points, project(2), 43), points.map((point) => [point]));
});

test("вблизи контуры и линии убирают перекрывающиеся точки, независимые остаются", () => {
  const project = ([lat, lon]) => ({ x: (lon - 20) * 100000, y: (45 - lat) * 100000 });
  const ring = [[44.8, 20.4], [44.8, 20.404], [44.804, 20.404], [44.804, 20.4], [44.8, 20.4]];
  const hole = [[44.801, 20.401], [44.801, 20.403], [44.803, 20.403],
    [44.803, 20.401], [44.801, 20.401]];
  const road = incident({ id: roadId, kind: "road", areas: [
    { scope: "road", rings: [ring, hole], label: "Контур" },
  ], lines: [{ coordinates: [[44.81, 20.4], [44.81, 20.41]], label: "Линия" }] });
  const inside = { lat: 44.8005, lon: 20.4005 };
  const onLine = { lat: 44.81004, lon: 20.405 };
  const inHole = { lat: 44.802, lon: 20.402 };
  const uncovered = { lat: 44.82, lon: 20.415 };
  assert.equal(road.areas[0].scope, "road");
  assert.deepEqual(pointsAtScale(
    [inside, onLine, inHole, uncovered], road.lines, road.areas, 17, project,
  ), [inHole, uncovered]);
  const overview = pointsAtScale([inside, uncovered], road.lines, road.areas, 13, project);
  assert.equal(overview.length, 3);
  assert.equal(overview.at(-1).label, "Линия");
  assert.equal(overview.at(-1).lat, 44.81);
  assert.deepEqual(pointsAtScale([inside], [], road.areas, 13, project), [inside]);
});

test("непроверенные координаты и небезопасные ссылки не попадают на карту", () => {
  assert.equal(incident({ source_url: "javascript:alert(1)" }), undefined);
  const event = incident({
    points: [{ lat: 55.75, lon: 37.61 }, { lat: 44.818, lon: 20.46 }],
    lines: [{ coordinates: [[44.818, 20.46], [55.75, 37.61]] }],
    source_map_url: "http://example.org/",
  });
  assert.equal(event.points.length, 1);
  assert.equal(event.lines.length, 0);
  assert.equal(event.officialMap, "");
});

test("контур дома не скрывает соседний точный адрес или его отдельное окно", () => {
  const project = ([lat, lon]) => ({ x: (lon - 20) * 100000, y: (45 - lat) * 100000 });
  const address = { municipality: "Врачар", street: "Жичка", number: "5" };
  const window = { start: 1, end: 2 };
  const area = { rings: [[[44.8, 20.4], [44.8, 20.401], [44.801, 20.4], [44.8, 20.4]]],
    address, window };
  const matched = { lat: 44.8, lon: 20.4, address, window };
  const adjacent = { ...matched, lon: 20.39997, address: { ...address, number: "7" } };
  const later = { ...matched, window: { start: 3, end: 4 } };
  assert.deepEqual(pointsAtScale([matched, adjacent, later], [], [area], 19, project),
    [adjacent, later]);
});


test("ссылка фестиваля открывает все площадки, повторное место различается датой", () => {
  const group = "event_aaaaaaaaaaaaaaaaaaaa";
  const rows = Array.from({ length: 6 }, (_, i) => ({
    id: "event_" + String(i + 1).padStart(20, "0"), group_id: group, kind: "event",
    title: "Чукафест", timezone: "Europe/Belgrade", source_url: "https://www.cukarica.rs/",
    starts_at: `2026-12-0${i + 2}T20:00:00+01:00`, ends_at: null,
    expires_at: `2026-12-0${i + 3}T00:00:00+01:00`,
    venue: { name: i === 3 ? "Площадка2" : "Площадка" + i, address: "Адрес", latitude: 44.78, longitude: 20.41 },
  }));
  const places = readPlaces({ version: 1, events: rows });
  assert.equal(launchPlaces(places, group).length, 6);
  assert.equal(new Set(places.map((place) => place.venue)).size, 5);
  assert.deepEqual(launchPlaces(places, rows[3].id), [places[3]]);
  assert.notEqual(placeOptionLabel(places[2]), placeOptionLabel(places[3]));
  assert.deepEqual(launchPlaces(places, "invalid"), []);
  const calendar = buildCalendar(places[3], Date.parse("2026-11-19T00:00:00Z"));
  const unfolded = calendar.replace(/\r\n /g, "");
  assert.match(unfolded, /LOCATION:Площадка2\\, Адрес/);
  assert.match(calendar, /DTSTART:20261205T190000Z/);
  assert.doesNotMatch(calendar, /DTEND/);
});

test("share link opens the Telegram share sheet with the same Mini App card", () => {
  const link = new URL(shareLink(waterId, "  Нет воды на Врачаре  "));
  assert.equal(link.origin + link.pathname, "https://t.me/share/url");
  assert.equal(link.searchParams.get("url"), `https://t.me/relocant_media_belgrade_bot?startapp=${waterId}`);
  assert.equal(link.searchParams.get("text"), "Нет воды на Врачаре");
  assert.equal(new URL(shareLink(waterId, "", true)).searchParams.get("url"),
    `https://t.me/relocant_media_belgrade_bot?startapp=places_${waterId}`);
  assert.equal(new URL(shareLink(waterId)).searchParams.has("text"), false);
  assert.equal(new URL(shareLink(waterId, "x".repeat(500))).searchParams.get("text").length, 200);
  for (const bad of ["", "event_123", "javascript:alert(1)", null, `${waterId}&x=1`]) {
    assert.equal(shareLink(bad, "Заголовок"), "");
  }
  const launched = launchMap("", new URL(new URL(shareLink(waterId, "", true)).searchParams.get("url"))
    .searchParams.get("startapp"));
  assert.deepEqual(launched, { mode: "places", id: waterId });
});
