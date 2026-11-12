import {
  addressHits, addressWindowText, buildAddressIndex, exactMapPoints, incidentStatus,
  incidentTimes, kinds, launchEvent, readIncidents, searchAddresses, streetBounds, updateStamp,
} from "./core.js";

const byId = (id) => document.getElementById(id);
const input = byId("address-query");
const searchResults = byId("address-results");
const eventList = byId("event-list");
const feedback = byId("feedback");
const archive = byId("archive");
const mapFrame = byId("map").parentElement;
const layerButtons = [...byId("layer-filter").querySelectorAll("button")];
const telegram = globalThis.Telegram?.WebApp;
const initialEvent = launchEvent(location.search, telegram?.initDataUnsafe?.start_param);
const center = [44.8186, 20.4609];
const region = [[44.22, 19.65], [45.23, 21.35]];
const mobile = matchMedia("(max-width: 740px)");
const animate = !matchMedia("(prefers-reduced-motion: reduce)").matches;
const enabled = new Set(Object.keys(kinds));
const state = {
  incidents: [],
  selected: "",
  address: null,
  index: null,
  loaded: false,
  loadFailed: false,
  deepPending: initialEvent,
  map: null,
  eventLayer: null,
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
  state.addressLayer = leaflet.layerGroup().addTo(map);
  byId("reset-map").addEventListener("click", () => {
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
    && (archive.checked || incidentStatus(incident, now) !== "closed"));
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
  if (incident.addresses.length) {
    const matches = new Set(addressHits(incident, state.address));
    const addresses = make("details", "event-scope");
    addresses.open = Boolean(matches.size || state.selected === incident.id);
    addresses.append(make("summary", "", `Адреса в объявлении · ${incident.addresses.length}`));
    const list = make("ul");
    for (const address of incident.addresses) {
      const district = address.municipality ? ` · ${address.municipality}` : "";
      const time = addressWindowText(address.window);
      const row = make("li", matches.has(address) ? "event-address-hit" : "",
        `${addressLabel(address)}${district}${time ? ` · ${time}` : ""}`);
      list.append(row);
    }
    addresses.append(list);
    card.append(addresses);
  }
  const links = make("div", "event-links");
  links.append(link(incident.source, "Первоисточник ↗"));
  if (incident.officialMap) links.append(link(incident.officialMap, "Официальная карта ↗"));
  card.append(links);
  return card;
}

function renderList(now) {
  const items = orderedIncidents(now);
  const count = byId("event-count");
  count.textContent = state.loaded ? String(items.length) : "";
  eventList.replaceChildren(...items.map((item) => eventCard(item, now)));
  const message = state.loadFailed
    ? state.loaded ? "Обновление недоступно. Показана сохранённая версия." : "Список объявлений не загрузился."
    : state.deepPending && state.loaded ? "Объявление из ссылки пока отсутствует в выгрузке."
      : !state.loaded ? "Загружаем данные."
        : !items.length ? "В выбранных слоях пока нет объявлений." : "";
  feedback.textContent = message;
  feedback.hidden = !message;
}

function renderPoints(now) {
  if (!state.map) return;
  const leaflet = globalThis.L;
  state.eventLayer.clearLayers();
  for (const incident of visibleIncidents(now)) {
    const color = kinds[incident.kind].color;
    const selected = incident.id === state.selected;
    const archived = incidentStatus(incident, now) === "closed";
    const handle = (layer, label) => {
      const popup = make("div");
      popup.append(make("strong", "", incident.title));
      if (label && label !== incident.title) popup.append(make("p", "", label));
      popup.append(link(incident.source, "Первоисточник ↗"));
      layer.bindPopup(popup).on("click", () => {
        state.selected = incident.id;
        state.focusNote = "";
        showMapNote();
        renderList(Date.now());
      }).addTo(state.eventLayer);
    };
    for (const { coordinates, label } of incident.lines) {
      handle(leaflet.polyline(coordinates, {
        color, opacity: archived ? .65 : .92, weight: selected ? 8 : 5, lineCap: "round",
      }), label);
    }
    for (const { lat, lon, label } of exactMapPoints(state.index, incident)) {
      handle(leaflet.circleMarker([lat, lon], {
        radius: selected ? 8 : 6, color: "#fffef9", weight: selected ? 3 : 2,
        fillColor: color, fillOpacity: archived ? .67 : .97,
      }), label);
    }
  }
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
    && addressHits(event, value).length);
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
  const now = Date.now();
  renderAddress();
  renderList(now);
  renderPoints(now);
}

function focusIncident(incident) {
  if (!state.map) return false;
  state.map.closePopup();
  const locations = exactMapPoints(state.index, incident).map(({ lat, lon }) => [lat, lon]);
  for (const line of incident.lines) {
    for (const point of line.coordinates) locations.push(point);
  }
  if (locations.length === 1) {
    state.map.setView(locations[0], 17, { animate });
  } else if (locations.length > 1) {
    state.map.fitBounds(locations, { padding: [30, 30], maxZoom: 17, animate });
  } else {
    const bounds = streetBounds(state.index, incident);
    if (bounds) {
      state.map.fitBounds([[bounds[0], bounds[1]], [bounds[2], bounds[3]]],
        { padding: [30, 30], maxZoom: 16, animate });
      state.focusNote = "Показана улица. Границы участка указаны в объявлении.";
      showMapNote();
      return true;
    }
    state.focusNote = incident.officialMap
      ? "Схема участка доступна по ссылке «Официальная карта»."
      : "Геометрия участка недоступна. Подробности в объявлении.";
    showMapNote();
    return false;
  }
  state.focusNote = "";
  showMapNote();
  return true;
}

function openEvent(incident, { fromCard = false } = {}) {
  state.deepPending = "";
  state.selected = incident.id;
  enabled.add(incident.kind);
  for (const button of layerButtons) {
    button.setAttribute("aria-pressed", String(enabled.has(button.dataset.kind)));
  }
  if (incidentStatus(incident, Date.now()) === "closed") archive.checked = true;
  render();
  const focused = focusIncident(incident);
  if (fromCard && mobile.matches && focused) {
    mapFrame.scrollIntoView({ behavior: animate ? "smooth" : "auto", block: "nearest" });
  } else if (fromCard) {
    eventList.querySelector('[data-selected="true"] button')?.focus({ preventScroll: true });
  }
}

let refreshTimer;
let deepAttempts = 0;
async function loadEvents() {
  const refresh = byId("refresh");
  if (refresh.disabled) return;
  clearTimeout(refreshTimer);
  refresh.disabled = true;
  try {
    const data = await readJSON("./incidents.json", { cache: "no-cache" });
    state.incidents = readIncidents(data);
    state.loaded = true;
    state.loadFailed = false;
    const stamp = updateStamp(data.generated_at);
    byId("sync-stamp").textContent = stamp ? `${stamp} · Белград` : "Обновлено";
    const pending = state.incidents.find((event) => event.id === state.deepPending);
    if (pending) openEvent(pending);
    else {
      render();
      if (state.deepPending && deepAttempts < 2) {
        deepAttempts += 1;
        refreshTimer = setTimeout(loadEvents, deepAttempts === 1 ? 7000 : 18000);
      }
    }
  } catch {
    state.loadFailed = true;
    byId("sync-stamp").textContent = "Повторить загрузку";
    renderList(Date.now());
  } finally {
    refresh.disabled = false;
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
    const event = state.incidents.find((incident) => incident.id === state.selected);
    if (event && !state.address && state.focusNote) focusIncident(event);
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
  state.deepPending = "";
  state.focusNote = "";
  input.value = addressLabel(value);
  hideSearch();
  render();
  showMapNote();
  state.map?.setView([value.lat, value.lon], 17, { animate });
  if (mobile.matches) {
    input.blur();
    mapFrame.scrollIntoView({ behavior: animate ? "smooth" : "auto", block: "nearest" });
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
archive.addEventListener("change", render);
byId("refresh").addEventListener("click", () => {
  deepAttempts = 0;
  void loadEvents();
});

setupMap();
telegram?.ready?.();
void loadEvents();
void loadAddressIndex();
