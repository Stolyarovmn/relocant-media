import assert from "node:assert/strict";
import { test } from "node:test";

import { buildCalendar } from "../web/newsroom/map/calendar.js";
import {
  addressHits, addressWindowText, buildAddressIndex, clusterMapPoints, exactMapPoints, incidentCountdown,
  incidentStatus, incidentTimes, launchDigestEvents, launchEvent, launchMap, nextTransition,
  placeCountdown, placeStatus,
  pointsAtScale, readIncidents, readPlaces, searchAddresses, straightLineDistance, streetBounds,
  unexpired, visibleMapLines,
} from "../web/newsroom/map/core.js";

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
