import {
  addressHits, addressWindowText, buildAddressIndex, clusterMapPoints, exactMapPoints, incidentCountdown,
  incidentStatus, incidentTimes, kinds, launchDigestEvents, launchMap, nextTransition, readIncidents,
  pointsAtScale, searchAddresses, streetBounds, unexpired, updateStamp, visibleMapLines,
} from "./core.js?v=6";
import { setupPlaces } from "./places.js?v=5";

const byId = (id) => document.getElementById(id);
const input = byId("address-query");
const searchResults = byId("address-results");
const eventList = byId("event-list");
const feedback = byId("feedback");
const archive = byId("archive");
const mapFrame = byId("map").parentElement;
const layerButtons = [...byId("layer-filter").querySelectorAll("button")];
const announcementBody = byId("announcement-body");
const announcementToggle = byId("toggle-announcements");
const selectedPreview = byId("selected-preview");
const telegram = globalThis.Telegram?.WebApp;
const initialRoute = launchMap(location.search, telegram?.initDataUnsafe?.start_param);
const digestIds = launchDigestEvents(location.search, telegram?.initDataUnsafe?.start_param);
const center = [44.8186, 20.4609];
const region = [[44.22, 19.65], [45.23, 21.35]];
const mobile = matchMedia("(max-width: 740px)");
const animate = !matchMedia("(prefers-reduced-motion: reduce)").matches;
const enabled = new Set(Object.keys(kinds));
const state = {
  incidents: [],
  selected: "",
  previewArchive: "",
  refocusEvent: "",
  address: null,
  index: null,
  loaded: false,
  loadFailed: false,
  deepPending: initialRoute.mode === "incidents" ? initialRoute.id : "",
  digestIds,
  mode: "",
  stamp: "",
  loading: false,
  map: null,
  eventLayer: null,
  pointLayer: null,
  mapItems: [],
  addressLayer: null,
  focusNote: "",
  tileStatus: "loading",
};

const statuses = {
  active: "Действует",
  planned: "Запланировано",
  between: "Между окнами",
  unknown: "Время уточняется",
  closed: "Завершено",
};
const statusOrder = { active: 0, planned: 1, between: 2, unknown: 3, closed: 4 };

function make(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text) node.textContent = text;
  return node;
}

function link(url, text) {
  const node = make("a", "", text);
  node.href = url;
  node.target = "_blank";
  node.rel = "noopener noreferrer";
  return node;
}

function readJSON(path, options) {
  return fetch(path, options).then((response) => {
    if (!response.ok) throw new Error("Данные недоступны");
    return response.json();
  });
}

function setupMap() {
  const leaflet = globalThis.L;
  if (!leaflet) {
    byId("reset-map").hidden = true;
    showMapNote();
    return;
  }
  const map = leaflet.map("map", {
    maxBounds: region, minZoom: 9, maxZoom: 19, zoomAnimation: animate,
    fadeAnimation: animate, markerZoomAnimation: animate, attributionControl: true,
  }).setView(center, 12);
  state.map = map;
  map.attributionControl.setPrefix(false);
  const tiles = leaflet.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    noWrap: true,
    bounds: region,
    attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(map);
  tiles.on("tileerror", () => {
    state.tileStatus = "unavailable";
    showMapNote();
  });
  tiles.on("tileload", () => {
    if (state.tileStatus !== "loaded") {
      state.tileStatus = "loaded";
      showMapNote();
    }
  });
  setTimeout(() => {
    if (state.tileStatus === "loading") {
      state.tileStatus = "unavailable";
      showMapNote();
    }
  }, 12000);
  state.eventLayer = leaflet.layerGroup().addTo(map);
  state.pointLayer = leaflet.layerGroup().addTo(map);
  state.addressLayer = leaflet.layerGroup().addTo(map);
  map.on("zoomend", renderPointClusters);
  map.on("dragstart", () => { state.refocusEvent = ""; });
  new ResizeObserver(() => map.invalidateSize({ pan: false })).observe(mapFrame);
  byId("reset-map").addEventListener("click", () => {
    state.refocusEvent = "";
    map.setView(center, 12, { animate });
    state.focusNote = "";
    showMapNote();
  });
  showMapNote();
}

function showMapNote() {
  const tileNote = state.tileStatus === "unavailable" ? "Подложка временно недоступна."
    : state.tileStatus === "loading" ? "Загружаем подложку карты." : "";
  const text = !state.map
    ? "Карта недоступна. Список объявлений открыт ниже."
    : [state.focusNote, tileNote].filter(Boolean).join(" ");
  const note = byId("map-message");
  note.textContent = text;
  note.hidden = !text;
}

function visibleIncidents(now) {
  return state.incidents.filter((incident) => enabled.has(incident.kind)
    && (!state.digestIds.length || state.digestIds.includes(incident.id))
    && (showsArchived(incident) || incidentStatus(incident, now) !== "closed"));
}

function showsArchived(incident) {
  return archive.checked || state.previewArchive === incident.id;
}

function orderedIncidents(now) {
  return visibleIncidents(now).sort((a, b) => {
    if (a.id === state.selected) return -1;
    if (b.id === state.selected) return 1;
    const hit = Number(Boolean(addressHits(b, state.address).length))
      - Number(Boolean(addressHits(a, state.address).length));
    return hit || statusOrder[incidentStatus(a, now)] - statusOrder[incidentStatus(b, now)]
      || (b.published ?? b.start ?? 0) - (a.published ?? a.start ?? 0);
  });
}

function addressLabel(address) {
  return address.number ? `${address.street}, ${address.number}` : address.street;
}

function eventCard(incident, now) {
  const card = make("article", "event-card");
  card.dataset.kind = incident.kind;
  card.dataset.id = incident.id;
  card.dataset.selected = String(state.selected === incident.id);
  const status = incidentStatus(incident, now);
  const meta = make("div", "event-meta");
  const category = make("span", "event-type");
  const dot = make("i");
  dot.setAttribute("aria-hidden", "true");
  category.append(dot, kinds[incident.kind].label);
  const badge = make("span", "event-state", incident.archived ? "Архив" : statuses[status]);
  badge.dataset.status = status;
  meta.append(category, badge);
  const heading = make("h3");
  const title = make("button", "event-focus", incident.title);
  title.type = "button";
  title.setAttribute("aria-pressed", card.dataset.selected);
  title.addEventListener("click", () => {
    openEvent(incident, { fromCard: true });
  });
  heading.append(title);
  card.append(meta, heading);
  const countdown = make("p", "event-countdown", incidentCountdown(incident, now));
  countdown.hidden = !countdown.textContent;
  card.append(countdown);

  const times = incidentTimes(incident);
  if (times.length === 1) card.append(make("p", "event-when", times[0]));
  if (times.length > 1) {
    const details = make("details", "event-when");
    details.append(make("summary", "", `Время работ · окон ${times.length}`));
    const windows = make("ul", "event-windows");
    for (const time of times) windows.append(make("li", "", time));
    details.append(windows);
    card.append(details);
  }
  for (const segment of incident.segments) {
    const extent = segment.start && segment.end ? ` от ${segment.start} до ${segment.end}` : "";
    const district = segment.municipality ? ` · ${segment.municipality}` : "";
    card.append(make("p", "event-scope", `${segment.street}${extent}${district}`));
  }
  for (const settlement of incident.settlements) {
    const scope = settlement.parent ? `${settlement.name} · часть ${settlement.parent}`
      : settlement.name;
    card.append(make("p", "event-scope", `${scope} · ${settlement.municipality} · ${addressWindowText(settlement.window)}`));
  }
  if (roadOrientation(incident)) {
    card.append(make("p", "event-scope", "Точные границы участка не подтверждены."));
  }
  if (incident.settlements.length && !incident.areas.some((area) => area.scope === "settlement")) {
    card.append(make("p", "event-scope", "Граница указанной территории не подтверждена."));
  }
  if (incident.addresses.length) {
    const matches = new Set(addressHits(incident, state.address));
    const addresses = make("details", "event-scope");
    addresses.open = Boolean(matches.size || state.selected === incident.id);
    const caption = incident.kind === "water" ? "Дома на участке"
      : incident.ranges.length ? "Дома из списка и диапазонов" : "Адреса в объявлении";
    addresses.append(make("summary", "", `${caption} · ${incident.addresses.length}`));
    const list = make("ul");
    for (const address of incident.addresses) {
      const district = address.municipality ? ` · ${address.municipality}` : "";
      const time = addressWindowText(address.window);
      const row = make("li", matches.has(address) ? "event-address-hit" : "",
        `${addressLabel(address)}${district}${time ? ` · ${time}` : ""}`);
      if (!unexpired(address.window, now)) row.dataset.expired = "true";
      list.append(row);
    }
    addresses.append(list);
    card.append(addresses);
  }
  if (incident.ranges.length) {
    const ranges = make("details", "event-scope");
    ranges.append(make("summary", "", `Диапазоны в источнике · ${incident.ranges.length}`));
    const list = make("ul");
    for (const range of incident.ranges) {
      list.append(make("li", "", `${addressLabel(range)} · ${range.municipality} · ${addressWindowText(range.window)}`));
    }
    ranges.append(list);
    card.append(ranges);
  }
  const links = make("div", "event-links");
  links.append(link(incident.source, "Первоисточник ↗"));
  if (incident.officialMap && incident.officialMap !== incident.source) {
    links.append(link(incident.officialMap,
      incident.kind === "road" ? "Схема секретариата ↗" : "Официальная карта ↗"));
  }
  card.append(links);
  return card;
}

function renderList(now) {
  const items = orderedIncidents(now);
  const count = byId("event-count");
  count.textContent = state.loaded ? String(items.length) : "";
  eventList.replaceChildren(...items.map((item) => eventCard(item, now)));
  const selected = items.find((item) => item.id === state.selected);
  selectedPreview.hidden = !selected;
  if (selected) {
    selectedPreview.dataset.kind = selected.kind;
    byId("selected-title").textContent = selected.title;
  }
  byId("selected-countdown").textContent = selected ? incidentCountdown(selected, now) : "";
  const message = state.loadFailed
    ? state.loaded ? "Обновление недоступно. Показана сохранённая версия." : "Список объявлений не загрузился."
    : state.deepPending && state.loaded ? "Объявление из ссылки пока отсутствует в выгрузке."
      : !state.loaded ? "Загружаем данные."
        : !items.length ? "В выбранных слоях пока нет объявлений." : "";
  feedback.textContent = message;
  feedback.hidden = !message;
}

function roadOrientation(incident) {
  return incident.kind === "road" && !incident.lines.length && !incident.areas.length;
}

function bindFeature(layer, group, incident, label, window, fromRegister) {
  const popup = make("div");
  popup.append(make("strong", "", incident.title));
  if (label && label !== incident.title) popup.append(make("p", "", label));
  if (window) popup.append(make("p", "", addressWindowText(window)));
  if (roadOrientation(incident)) {
    popup.append(make("p", "", "Ориентир. Точные границы перекрытия не подтверждены."));
  }
  popup.append(link(incident.source, "Первоисточник ↗"));
  if (fromRegister) {
    const credit = make("p");
    credit.append(link("https://data.gov.rs/sr/datasets/adresni-registar/", "Координаты · реестр Сербии ↗"));
    popup.append(credit);
  }
  layer.bindPopup(popup).on("click", () => {
    state.selected = incident.id;
    if (incidentStatus(incident, Date.now()) !== "closed") state.previewArchive = "";
    state.refocusEvent = "";
    state.focusNote = "";
    showMapNote();
    renderList(Date.now());
  }).addTo(group);
}

function drawCluster(points, zoom) {
  const leaflet = globalThis.L;
  const [first] = points;
  const location = [first.lat, first.lon];
  const kindsInCluster = new Set(points.map(({ incident }) => incident.kind));
  const badge = make("span", "map-cluster-count", String(points.length));
  badge.setAttribute("aria-hidden", "true");
  if (kindsInCluster.size === 1) badge.dataset.kind = first.incident.kind;
  const title = `Метки на карте · ${points.length}`;
  const size = mobile.matches ? 44 : 38;
  const icon = leaflet.divIcon({
    className: "map-cluster", html: badge, iconSize: [size, size],
  });
  const marker = leaflet.marker(location, { icon, title, alt: title, riseOnHover: true });
  marker.on("add", () => marker.getElement()?.setAttribute("aria-label", title));

  const sameLocation = points.every((point) => point.lat === first.lat && point.lon === first.lon);
  if (zoom < state.map.getMaxZoom() && !sameLocation) {
    marker.on("click", () => {
      const next = Math.min(zoom + 2, state.map.getMaxZoom());
      const bounds = leaflet.latLngBounds(points.map(({ lat, lon }) => [lat, lon]));
      if (state.map.getBoundsZoom(bounds, false, [70, 70]) > zoom) {
        state.map.fitBounds(bounds, { padding: [35, 35], maxZoom: next, animate });
      } else {
        state.map.setView(location, next, { animate });
      }
    });
  } else {
    const popup = make("div", "map-cluster-popup");
    popup.append(make("strong", "", title));
    const incidents = new Map(points.map(({ incident }) => [incident.id, incident]));
    const list = make("ul");
    for (const incident of incidents.values()) {
      const row = make("li");
      const button = make("button", "", incident.title);
      button.type = "button";
      button.addEventListener("click", () => openEvent(incident));
      row.append(button);
      list.append(row);
    }
    popup.append(list);
    marker.bindPopup(popup);
  }
  marker.addTo(state.pointLayer);
}

function roadLineWeight(zoom, selected) {
  return Math.max(7, 7 + (zoom - 13) * 2) + (selected ? 3 : 0);
}

function renderPointClusters() {
  if (!state.map || state.mode !== "incidents") return;
  const leaflet = globalThis.L;
  const zoom = state.map.getZoom();
  const project = (coordinates) => state.map.project(coordinates, zoom);
  state.pointLayer.clearLayers();
  const markers = state.mapItems.flatMap(({ incident, lines, areas, points, roadPaths, archived, now }) => {
    for (const path of roadPaths) {
      path.setStyle({ weight: roadLineWeight(zoom, incident.id === state.selected) });
    }
    return pointsAtScale(points, lines, areas, zoom, project)
      .map((point) => ({ ...point, incident, archived, now }));
  });
  for (const group of clusterMapPoints(markers, project, mobile.matches ? 49 : 43)) {
    if (group.length > 1) {
      drawCluster(group, zoom);
      continue;
    }
    const [point] = group;
    const { lat, lon, label, address, window, fromRegister, incident, archived, now } = point;
    const selected = incident.id === state.selected;
    const expired = !unexpired(window || address?.window, now);
    bindFeature(leaflet.circleMarker([lat, lon], {
      radius: selected ? 8 : 6, color: "#fffef9", weight: selected ? 3 : 2,
      fillColor: kinds[incident.kind].color, fillOpacity: archived || expired ? .58 : .96,
      className: "outage-point",
    }), state.pointLayer, incident, label, window || address?.window, fromRegister);
  }
}

function renderPoints(now) {
  if (!state.map || state.mode !== "incidents") return;
  const leaflet = globalThis.L;
  state.eventLayer.clearLayers();
  state.mapItems = visibleIncidents(now).map((incident) => {
    const includeExpired = showsArchived(incident);
    const lines = visibleMapLines(incident, now, includeExpired);
    const areas = incident.areas.filter((area) => unexpired(area.window, now, includeExpired));
    const points = exactMapPoints(state.index, incident, now, includeExpired);
    const color = kinds[incident.kind].color;
    const selected = incident.id === state.selected;
    const archived = incidentStatus(incident, now) === "closed";
    const road = incident.kind === "road";
    const roadPaths = [];
    for (const { coordinates, label, window } of lines) {
      const expired = archived || !unexpired(window, now);
      const style = road
        ? { opacity: expired ? .30 : selected ? .64 : .48, weight: roadLineWeight(state.map.getZoom(), selected) }
        : { opacity: expired ? .48 : .94, weight: selected ? 8 : 5 };
      const path = leaflet.polyline(coordinates, {
        color, ...style, lineCap: "round", lineJoin: "round",
        className: road ? "outage-road" : "outage-line",
      });
      bindFeature(path, state.eventLayer, incident, label, window);
      if (road) roadPaths.push(path);
    }
    for (const area of areas) {
      const expired = archived || !unexpired(area.window, now);
      const fill = road ? selected ? .51 : .42 : area.scope === "settlement" ? .24 : selected ? .68 : .49;
      bindFeature(leaflet.polygon(area.rings, {
        color, fillColor: color, weight: selected ? 2.8 : 1.8,
        opacity: expired ? .50 : road ? .60 : .92,
        fillOpacity: expired ? .12 : fill,
        className: road ? "outage-road" : `outage-${area.scope}`,
      }), state.eventLayer, incident, area.label, area.window);
    }
    return { incident, lines, areas, points, roadPaths, archived, now };
  });
  renderPointClusters();
}

function renderAddress() {
  const picked = byId("picked-address");
  const value = state.address;
  picked.hidden = !value;
  state.addressLayer?.clearLayers();
  if (!value) return;
  byId("picked-label").textContent = addressLabel(value);
  const now = Date.now();
  const matches = state.incidents.filter((event) => incidentStatus(event, now) !== "closed"
    && addressHits(event, value).some((address) => unexpired(address.window, now)));
  byId("picked-match").textContent = matches.length
    ? `Точных совпадений в актуальных объявлениях: ${matches.length}`
    : "В актуальных объявлениях точного совпадения пока нет.";
  if (!state.map) return;
  globalThis.L.circleMarker([value.lat, value.lon], {
    radius: 10, color: "#19362d", weight: 3, fillColor: "#f4eea8", fillOpacity: 1,
  }).bindTooltip(make("span", "", addressLabel(value)), { direction: "top" })
    .addTo(state.addressLayer);
}

function render() {
  if (state.mode !== "incidents") return;
  const now = Date.now();
  renderAddress();
  renderList(now);
  renderPoints(now);
}

function focusIncident(incident) {
  if (!state.map) return false;
  state.map.closePopup();
  const now = Date.now();
  const includeExpired = showsArchived(incident);
  const locations = exactMapPoints(state.index, incident, now, includeExpired)
    .map(({ lat, lon }) => [lat, lon]);
  for (const line of visibleMapLines(incident, now, includeExpired)) {
    for (const point of line.coordinates) locations.push(point);
  }
  for (const area of incident.areas) {
    if (unexpired(area.window, now, includeExpired)) locations.push(...area.rings[0]);
  }
  const padding = mobile.matches
    ? { paddingTopLeft: [24, 155], paddingBottomRight: [25, 28] }
    : { padding: [30, 30] };
  if (locations.length === 1) {
    state.map.setView(locations[0], 17, { animate });
  } else if (locations.length > 1) {
    state.map.fitBounds(locations, { ...padding, maxZoom: 17, animate });
  } else {
    const bounds = incident.kind === "water" ? streetBounds(state.index, incident) : null;
    if (bounds) {
      state.map.fitBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]],
        { ...padding, maxZoom: 16, animate });
      state.focusNote = "Показана улица. Границы участка указаны в объявлении.";
      showMapNote();
      return true;
    }
    state.focusNote = incident.kind === "road" ? "Точные границы перекрытия не подтверждены."
      : incident.officialMap
      ? "Схема участка доступна по ссылке «Официальная карта»."
      : "Геометрия участка недоступна. Подробности в объявлении.";
    showMapNote();
    return false;
  }
  state.focusNote = roadOrientation(incident)
    ? "Показан ориентир. Точные границы перекрытия не подтверждены." : "";
  showMapNote();
  return true;
}

function focusDigest() {
  const now = Date.now();
  const locations = visibleIncidents(now).flatMap((incident) => [
    ...exactMapPoints(state.index, incident, now, false).map(({ lat, lon }) => [lat, lon]),
    ...visibleMapLines(incident, now, false).flatMap((line) => line.coordinates),
    ...incident.areas.filter((area) => unexpired(area.window, now, false)).flatMap((area) => area.rings[0]),
  ]);
  if (locations.length && state.map) state.map.fitBounds(locations, { padding: [35, 65], maxZoom: 16 });
}

function openEvent(incident, { fromCard = false } = {}) {
  const now = Date.now();
  state.deepPending = "";
  state.selected = incident.id;
  state.previewArchive = incidentStatus(incident, now) === "closed" ? incident.id : "";
  state.refocusEvent = !state.index && (incident.addresses.length || incident.segments.length)
    ? incident.id : "";
  enabled.add(incident.kind);
  for (const button of layerButtons) {
    button.setAttribute("aria-pressed", String(enabled.has(button.dataset.kind)));
  }
  render();
  const includeExpired = showsArchived(incident);
  if (fromCard && mobile.matches && (visibleMapLines(incident, now, includeExpired).length
      || incident.areas.some((area) => unexpired(area.window, now, includeExpired))
      || exactMapPoints(state.index, incident, now, includeExpired).length
      || streetBounds(state.index, incident))) {
    showAnnouncements(false);
    announcementToggle.focus({ preventScroll: true });
  } else if (fromCard) {
    eventList.querySelector('[data-selected="true"] button')?.focus({ preventScroll: true });
  }
  focusIncident(incident);
}

function showAnnouncements(open) {
  announcementToggle.setAttribute("aria-expanded", String(open));
  announcementBody.hidden = !open;
  state.map?.invalidateSize({ pan: false });
}

let refreshTimer;
let clockTimer;
let deepAttempts = 0;
function updateCountdowns(now) {
  for (const card of eventList.children) {
    const incident = state.incidents.find((entry) => entry.id === card.dataset.id);
    const countdown = card.querySelector(".event-countdown");
    countdown.textContent = incidentCountdown(incident, now);
    countdown.hidden = !countdown.textContent;
  }
  const selected = state.incidents.find((entry) => entry.id === state.selected);
  byId("selected-countdown").textContent = selected ? incidentCountdown(selected, now) : "";
}

function scheduleClock() {
  clearTimeout(clockTimer);
  if (state.mode !== "incidents" || document.hidden) return;
  const now = Date.now();
  const boundary = nextTransition(state.incidents, now);
  const nextMinute = 60_000 - now % 60_000;
  clockTimer = setTimeout(() => {
    const current = Date.now();
    if (current >= boundary) render();
    else updateCountdowns(current);
    scheduleClock();
  }, Math.min(nextMinute, boundary - now + 10));
}

async function loadEvents() {
  if (state.loading) return;
  clearTimeout(refreshTimer);
  state.loading = true;
  if (state.mode === "incidents") syncFeed(state);
  try {
    const data = await readJSON("./incidents.json", { cache: "no-cache" });
    state.incidents = readIncidents(data);
    state.loaded = true;
    state.loadFailed = false;
    state.stamp = updateStamp(data.generated_at);
    if (state.mode !== "incidents") return;
    const pending = state.incidents.find((event) => event.id === state.deepPending);
    if (pending) openEvent(pending);
    else {
      render();
      if (state.digestIds.length) focusDigest();
      if (state.deepPending && deepAttempts < 2) {
        deepAttempts += 1;
        refreshTimer = setTimeout(loadEvents, deepAttempts === 1 ? 7000 : 18000);
      }
    }
    scheduleClock();
  } catch {
    state.loadFailed = true;
    if (state.mode === "incidents") renderList(Date.now());
  } finally {
    state.loading = false;
    if (state.mode === "incidents") syncFeed(state);
  }
}

let addressRequest;
async function readAddresses() {
  if (typeof DecompressionStream === "function") {
    try {
      const response = await fetch("./addresses.json.gz");
      if (response.ok && response.body) {
        const json = response.body.pipeThrough(new DecompressionStream("gzip"));
        return await new Response(json).json();
      }
    } catch {
      // Keep address search available if the host or WebView cannot stream gzip.
    }
  }
  return readJSON("./addresses.json");
}

function loadAddressIndex() {
  addressRequest ??= readAddresses().then((data) => {
    state.index = buildAddressIndex(data);
    renderPoints(Date.now());
    const event = state.incidents.find((incident) => incident.id === state.refocusEvent);
    state.refocusEvent = "";
    if (state.mode === "incidents" && event && !state.address) focusIncident(event);
    if (state.mode === "incidents" && state.digestIds.length && !state.address && !state.selected) {
      focusDigest();
    }
    return state.index;
  }).catch(() => {
    addressRequest = null;
    return null;
  });
  return addressRequest;
}

function hideSearch() {
  searchResults.hidden = true;
}

function searchMessage(message) {
  const status = make("p", "", message);
  status.setAttribute("role", "status");
  searchResults.replaceChildren(status);
  searchResults.hidden = false;
}

function chooseAddress(value) {
  state.address = value;
  state.refocusEvent = "";
  state.deepPending = "";
  state.focusNote = "";
  input.value = addressLabel(value);
  hideSearch();
  render();
  showMapNote();
  state.map?.setView([value.lat, value.lon], 17, { animate });
  if (mobile.matches) {
    input.blur();
  } else {
    input.focus();
  }
}

let searchTimer;
let searchVersion = 0;
async function findAddress(submit) {
  const version = ++searchVersion;
  const query = input.value.trim();
  if (query.length < 3) {
    if (submit) searchMessage("Введите хотя бы три символа.");
    else hideSearch();
    return;
  }
  if (!state.index) searchMessage("Загружаем индекс адресов.");
  const index = state.index ?? await loadAddressIndex();
  if (version !== searchVersion || input.value.trim() !== query) return;
  if (!index) {
    searchMessage("Индекс адресов недоступен. Повторите поиск.");
    return;
  }
  const matches = searchAddresses(index, query, submit);
  if (!matches.length) {
    searchMessage("Адрес не найден. Проверьте улицу и номер дома.");
    return;
  }
  if (submit && matches.length === 1) {
    chooseAddress(matches[0]);
    return;
  }
  const attribution = make("p", "address-attribution");
  attribution.append(link("https://www.openstreetmap.org/copyright", "Адреса © OpenStreetMap"));
  searchResults.replaceChildren(...matches.map((match) => {
    const button = make("button", "", addressLabel(match));
    button.type = "button";
    if (match.municipality) button.append(make("small", "", match.municipality));
    button.addEventListener("click", () => {
      chooseAddress(match);
    });
    return button;
  }), attribution);
  searchResults.hidden = false;
  if (submit) searchResults.querySelector("button")?.focus();
}

byId("address-form").addEventListener("submit", (event) => {
  event.preventDefault();
  clearTimeout(searchTimer);
  void findAddress(true);
});
input.addEventListener("input", () => {
  searchVersion += 1;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => void findAddress(false), 160);
});
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || searchResults.hidden) return;
  searchVersion += 1;
  clearTimeout(searchTimer);
  hideSearch();
  input.focus();
});
document.addEventListener("pointerdown", (event) => {
  if (!byId("address-form").contains(event.target)) {
    searchVersion += 1;
    clearTimeout(searchTimer);
    hideSearch();
  }
});
byId("clear-address").addEventListener("click", () => {
  state.address = null;
  input.value = "";
  render();
  input.focus();
});
for (const button of layerButtons) {
  button.addEventListener("click", () => {
    const kind = button.dataset.kind;
    if (enabled.delete(kind)) {
      if (state.incidents.find((event) => event.id === state.selected)?.kind === kind) {
        state.selected = "";
        state.refocusEvent = "";
        state.focusNote = "";
        showMapNote();
      }
    } else {
      enabled.add(kind);
    }
    button.setAttribute("aria-pressed", String(enabled.has(kind)));
    render();
  });
}
archive.addEventListener("change", () => {
  if (!archive.checked) state.previewArchive = "";
  render();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) clearTimeout(clockTimer);
  else if (state.mode === "incidents") {
    render();
    scheduleClock();
  }
});
announcementToggle.addEventListener("click", () => showAnnouncements(announcementBody.hidden));
selectedPreview.addEventListener("click", () => {
  showAnnouncements(true);
  eventList.querySelector('[data-selected="true"] button')?.focus();
});
mobile.addEventListener("change", () => {
  showAnnouncements(!mobile.matches);
  renderPointClusters();
});
showAnnouncements(!mobile.matches);
byId("refresh").addEventListener("click", () => {
  if (state.mode === "places") {
    places.refresh();
    return;
  }
  deepAttempts = 0;
  void loadEvents();
});

function syncFeed(feed) {
  byId("refresh").disabled = feed.loading;
  byId("sync-stamp").textContent = feed.failed || feed.loadFailed ? "Повторить загрузку"
    : feed.stamp ? `${feed.stamp} · Белград`
      : feed.loaded ? "Обновлено" : "Загрузка";
}

const views = new Map();
function showMode(mode, id = "") {
  if (mode === state.mode) return;
  if (state.map && state.mode) {
    views.set(state.mode, { center: state.map.getCenter(), zoom: state.map.getZoom() });
    state.map.closePopup();
  }
  state.mode = mode;
  const exploring = mode === "places";
  document.body.dataset.mode = mode;
  byId("show-incidents").setAttribute("aria-pressed", String(!exploring));
  byId("show-places").setAttribute("aria-pressed", String(exploring));
  byId("incident-controls").hidden = exploring;
  byId("places-controls").hidden = !exploring;
  byId("announcements").hidden = exploring;
  byId("places").hidden = !exploring;
  const skip = document.querySelector(".skip-link");
  skip.href = exploring ? "#places" : "#announcements";
  skip.textContent = exploring ? "К списку событий" : "К списку объявлений";
  byId("refresh").setAttribute("aria-label", exploring ? "Обновить события" : "Обновить объявления");
  state.focusNote = "";
  showMapNote();
  state.map?.invalidateSize({ pan: false });
  const view = views.get(mode);
  state.map?.setView(view?.center ?? center, view?.zoom ?? 12, { animate: false });
  if (exploring) {
    clearTimeout(clockTimer);
    clearTimeout(refreshTimer);
    state.eventLayer?.clearLayers();
    state.pointLayer?.clearLayers();
    state.addressLayer?.clearLayers();
    places.enter(id);
  } else {
    places.leave();
    const pending = state.incidents.find((incident) => incident.id === state.deepPending);
    if (pending) openEvent(pending);
    else render();
    syncFeed(state);
    scheduleClock();
    if (!state.loaded || state.deepPending) void loadEvents();
    void loadAddressIndex();
  }
}

setupMap();
const places = setupPlaces({ map: state.map, mobile, animate, telegram, byId, make, link, readJSON, onSync: syncFeed });
byId("show-incidents").addEventListener("click", () => {
  state.digestIds = [];
  if (state.mode === "incidents") render();
  else showMode("incidents");
});
byId("show-places").addEventListener("click", () => showMode("places"));
telegram?.ready?.();
showMode(initialRoute.mode, initialRoute.id);
