import assert from "node:assert/strict";
import { test } from "node:test";

import {
  addressHits, addressWindowText, buildAddressIndex, exactMapPoints, incidentCountdown,
  incidentStatus, incidentTimes, launchEvent, nextTransition, readIncidents, searchAddresses,
  streetBounds, unexpired,
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

test("одиночная точка из старой выгрузки не обозначает дорожное перекрытие", () => {
  const road = incident({ id: roadId, kind: "road", points: [{ lat: 44.81, lon: 20.44 }] });
  assert.deepEqual(exactMapPoints(addressIndex, road), []);
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
