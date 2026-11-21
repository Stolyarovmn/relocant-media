import {
  clusterMapPoints, incidentTimes, launchPlaces, placeCountdown, placeOptionLabel, placeStatus, readPlaces,
  shareLink, straightLineDistance, updateStamp,
} from "./core.js?v=14";
import { calendarLink } from "./calendar.js?v=4";

const kilometers = new Intl.NumberFormat("ru", { maximumFractionDigits: 1 });
const courseStyles = [
  { color: "#076342", weight: 7 },
  { color: "#5827a9", weight: 4, dashArray: "9 7" },
  { color: "#155a9a", weight: 4, dashArray: "3 6" },
  { color: "#9c3c02", weight: 4, dashArray: "12 5 3 5" },
];

function distanceLabel(origin, place) {
  const meters = straightLineDistance(origin, place);
  if (meters < 10) return "Менее 10 м по прямой";
  return meters < 995
    ? `${Math.round(meters / 10) * 10} м по прямой`
    : `${kilometers.format(meters / 1000)} км по прямой`;
}

function validPosition(value) {
  return Number.isFinite(value?.latitude) && Math.abs(value.latitude) <= 90
    && Number.isFinite(value.longitude) && Math.abs(value.longitude) <= 180;
}

function browserPosition() {
  return new Promise((resolve, reject) => {
    if (!navigator.geolocation) {
      reject(new Error("unavailable"));
      return;
    }
    navigator.geolocation.getCurrentPosition(({ coords }) => resolve(coords),
      ({ code }) => reject(new Error(code === 1 ? "denied" : "unavailable")),
      { timeout: 12_000, maximumAge: 0 });
  });
}

function requestPosition(telegram) {
  const native = telegram?.LocationManager;
  if (!telegram?.isVersionAtLeast?.("8.0") || !native?.init || !native.getLocation) {
    return browserPosition();
  }
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error("unavailable")), 20_000);
    const finish = (value) => {
      clearTimeout(timeout);
      if (validPosition(value)) resolve(value);
      else reject(new Error("denied"));
    };
    const ready = () => {
      if (!native.isLocationAvailable) {
        clearTimeout(timeout);
        browserPosition().then(resolve, reject);
        return;
      }
      native.getLocation(finish);
    };
    if (native.isInited) ready();
    else native.init(ready);
  });
}

export function setupPlaces({ map, mobile, animate, telegram, byId, make, link, readJSON, onSync }) {
  const list = byId("places-list");
  const detail = byId("place-detail");
  const count = byId("places-count");
  const feedback = byId("places-feedback");
  const toggle = byId("toggle-places");
  const body = byId("places-body");
  const locate = byId("locate-places");
  const clearLocation = byId("clear-location");
  const locationStatus = byId("location-status");
  const leaflet = globalThis.L;
  const layer = map ? leaflet.layerGroup().addTo(map) : null;
  const courseLayer = map ? leaflet.layerGroup().addTo(map) : null;
  const state = {
    entries: [], selected: "", pending: "", group: "", enabled: false, loading: false,
    loaded: false, failed: false, stamp: "", origin: null,
  };
  let clock;
  let retry;
  let attempts = 0;

  function visiblePlaces(now) {
    return state.entries.filter((place) => !state.group || place.group === state.group)
      .filter((place) => place.paidPlace
      ? placeStatus(place, now) === "active"
      : place.id === state.selected || placeStatus(place, now) !== "closed");
  }

  function showList(open) {
    toggle.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
    map?.invalidateSize({ pan: false });
  }

  function placeTimes(place) {
    return incidentTimes({ ...place, windows: [], startDay: null, endDay: null })[0];
  }

  function describe(place, now) {
    const countdown = make("span", "place-countdown", placeCountdown(place, now));
    countdown.dataset.placeId = place.id;
    const when = make("span", "place-when", placeTimes(place));
    const venue = make("span", "place-venue", place.venue);
    const parts = place.paidPlace ? [venue] : [countdown, when, venue];
    if (place.visitorHours) parts.push(make("span", "place-when", place.visitorHours));
    if (place.scheduleNote) parts.push(make("span", "place-when", place.scheduleNote));
    if (place.sponsored) parts.unshift(make("span", "place-ad", "★ Реклама"));
    if (state.origin) parts.push(make("span", "place-distance", distanceLabel(state.origin, place)));
    return parts;
  }

  function card(place, now) {
    const button = make("button", "place-card");
    button.type = "button";
    button.dataset.placeId = place.id;
    button.setAttribute("aria-pressed", String(place.id === state.selected));
    button.append(make("strong", "", place.title), ...describe(place, now));
    button.addEventListener("click", () => openPlace(place, true));
    return button;
  }

  function renderDetail(place, now) {
    detail.hidden = !place;
    if (!place) {
      detail.replaceChildren();
      return;
    }
    const close = make("button", "place-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "Закрыть карточку события");
    close.addEventListener("click", () => {
      state.selected = "";
      render();
      if (body.hidden) toggle.focus();
      else [...list.children].find((node) => node.dataset.placeId === place.id)?.focus();
    });
    const links = make("div", "event-links");
    if (place.post) links.append(link(place.post, "Пост в Telegram ↗"));
    if (place.schedule) links.append(link(place.schedule, "Расписание ↗"));
    links.append(link(place.source, "Источник ↗"));
    const share = place.sponsored ? "" : shareLink(place.id, place.title, true);
    if (share) links.append(link(share, "Поделиться ↗"));
    const calendar = place.paidPlace || place.scheduleNote ? null : calendarLink(place, now);
    if (calendar) links.append(calendar);
    detail.replaceChildren(close, make("h3", "", place.title), ...describe(place, now));
    if (place.address && place.address !== place.venue) detail.append(make("p", "place-address", place.address));
    if (place.venueNote) detail.append(make("p", "place-address", place.venueNote));
    if (place.courses.length) {
      const legend = make("ul", "place-course-legend");
      legend.setAttribute("aria-label", "Трассы на карте");
      for (const [index, course] of place.courses.entries()) {
        const style = courseStyles[index];
        const sample = make("i", "place-course-sample");
        sample.setAttribute("aria-hidden", "true");
        sample.style.color = style.color;
        sample.style.borderTopStyle = style.dashArray ? "dashed" : "solid";
        const item = make("li");
        item.append(sample, make("span", "", course.name));
        legend.append(item);
      }
      detail.append(legend);
    }
    detail.append(links);
  }

  function renderCourses(place) {
    if (!courseLayer) return;
    courseLayer.clearLayers();
    if (!place) return;
    // Общая светлая подложка сохраняет контраст на светлой и цветной карте.
    for (const { latLngs } of place.courses) {
      leaflet.polyline(latLngs, { color: "#fffdf9", weight: 10, interactive: false }).addTo(courseLayer);
    }
    for (const [index, { latLngs }] of place.courses.entries()) {
      leaflet.polyline(latLngs, { ...courseStyles[index], interactive: false }).addTo(courseLayer);
    }
  }

  function mark(places, zoom) {
    const [first] = places;
    const multiple = places.length > 1;
    const sponsored = !multiple && first.sponsored;
    const title = multiple ? `События рядом · ${places.length}`
      : sponsored ? `Реклама · ${first.title}` : first.title;
    const badge = make("span", "place-pin-badge", multiple ? String(places.length) : "");
    badge.setAttribute("aria-hidden", "true");
    const active = places.some((place) => place.id === state.selected);
    const icon = leaflet.divIcon({
      className: `place-pin${multiple ? " place-pin-group" : ""}${active ? " place-pin-selected" : ""}${sponsored ? " place-pin-ad" : ""}`,
      html: badge, iconSize: [38, 38], iconAnchor: [19, 19],
    });
    const location = [first.lat, first.lon];
    const marker = leaflet.marker(location, { icon, title, alt: title, riseOnHover: true });
    marker.on("add", () => marker.getElement()?.setAttribute("aria-label", title));
    if (!multiple) {
      marker.on("click", () => openPlace(first));
    } else if (zoom < map.getMaxZoom()
        && places.some(({ lat, lon }) => lat !== first.lat || lon !== first.lon)) {
      marker.on("click", () => {
        const bounds = leaflet.latLngBounds(places.map(({ lat, lon }) => [lat, lon]));
        map.fitBounds(bounds, { padding: [38, 38], maxZoom: Math.min(zoom + 2, map.getMaxZoom()), animate });
      });
    } else {
      const popup = make("div", "map-cluster-popup");
      const options = make("ul");
      for (const place of places) {
        const row = make("li");
        const button = make("button", "", `${place.title} · ${placeOptionLabel(place)}`);
        button.type = "button";
        button.addEventListener("click", () => openPlace(place));
        row.append(button);
        options.append(row);
      }
      popup.append(make("strong", "", title), options);
      marker.bindPopup(popup);
    }
    marker.addTo(layer);
  }

  function renderMarkers() {
    if (!layer) return;
    layer.clearLayers();
    if (!state.enabled) return;
    const zoom = map.getZoom();
    const project = (coordinates) => map.project(coordinates, zoom);
    for (const group of clusterMapPoints(visiblePlaces(Date.now()), project, 40)) mark(group, zoom);
  }

  function render() {
    if (!state.enabled) return;
    const now = Date.now();
    const entries = visiblePlaces(now);
    entries.sort((a, b) => a.start - b.start);
    if (state.origin) {
      const distances = new Map(entries.map((place) => [place.id, straightLineDistance(state.origin, place)]));
      entries.sort((a, b) => distances.get(a.id) - distances.get(b.id));
    }
    count.textContent = state.loaded ? String(entries.length) : "";
    list.replaceChildren(...entries.map((place) => card(place, now)));
    const selected = entries.find((place) => place.id === state.selected);
    renderDetail(selected, now);
    renderCourses(selected);
    const message = state.failed
      ? state.loaded ? "Афиша не обновилась. Показана предыдущая версия." : "Афиша не загрузилась. Попробуйте обновить."
      : !state.loaded ? "Загружаем события."
        : state.pending ? "Событие из ссылки пока отсутствует в афише."
          : !entries.length ? "В афише пока нет предстоящих событий."
            : state.group ? `Площадок: ${new Set(entries.map((place) => `${place.lat},${place.lon}`)).size}. Выберите концерт в списке или на карте.` : "";
    feedback.textContent = message;
    if (state.group && !state.failed && !state.pending) {
      const reset = make("button", "", "Все события");
      reset.type = "button";
      reset.addEventListener("click", () => {
        state.group = "";
        state.selected = "";
        render();
      });
      feedback.append(document.createTextNode(" "), reset);
    }
    feedback.hidden = !message;
    renderMarkers();
  }

  function openPlace(place, fromList = false) {
    state.selected = place.id;
    state.pending = "";
    map?.closePopup();
    if (mobile.matches) showList(false);
    render();
    if (fromList) detail.querySelector("button")?.focus({ preventScroll: true });
    map?.invalidateSize({ pan: false });
    if (map && place.courses.length) {
      const bounds = leaflet.latLngBounds([
        [place.lat, place.lon], ...place.courses.flatMap(({ latLngs }) => latLngs),
      ]);
      map.fitBounds(bounds, { paddingTopLeft: [32, 80], paddingBottomRight: [32, 35],
        maxZoom: 16, animate });
    } else map?.setView([place.lat, place.lon], Math.max(map.getZoom(), 16), { animate });
  }

  function scheduleClock() {
    clearTimeout(clock);
    if (!state.enabled || document.hidden) return;
    const now = Date.now();
    let next = Infinity;
    for (const place of state.entries) {
      if (place.archived) continue;
      for (const time of [place.start, place.end, place.expires]) {
        if (time !== null && time > now) next = Math.min(next, time);
      }
    }
    clock = setTimeout(() => {
      const current = Date.now();
      if (current >= next) render();
      else {
        const entries = new Map(state.entries.map((place) => [place.id, place]));
        for (const node of byId("places").querySelectorAll(".place-countdown")) {
          node.textContent = placeCountdown(entries.get(node.dataset.placeId), current);
        }
      }
      scheduleClock();
    }, Math.min(60_000 - now % 60_000, next - now + 10));
  }

  async function load() {
    if (state.loading) return;
    clearTimeout(retry);
    state.loading = true;
    if (state.enabled) onSync(state);
    try {
      const data = await readJSON("./events.json", { cache: "no-cache" });
      state.entries = readPlaces(data);
      state.stamp = updateStamp(data.generated_at);
      state.loaded = true;
      state.failed = false;
    } catch {
      state.failed = true;
    } finally {
      state.loading = false;
      if (state.enabled) show();
    }
    if (state.enabled && state.pending && !state.failed && attempts < 2) {
      attempts += 1;
      retry = setTimeout(load, attempts === 1 ? 7000 : 18000);
    }
  }

  function show() {
    const selected = state.pending && state.entries.find((place) => place.id === state.pending);
    const group = launchPlaces(state.entries, state.pending);
    if (selected) {
      state.group = selected.group;
      openPlace(selected);
    } else if (group?.length) {
      state.group = state.pending;
      state.pending = "";
      state.selected = "";
      render();
      showList(true);
      map?.fitBounds(leaflet.latLngBounds(group.map(({ lat, lon }) => [lat, lon])),
        { padding: [35, 35], maxZoom: 15, animate });
    } else render();
    onSync(state);
    scheduleClock();
  }

  function setLocationStatus(message) {
    locationStatus.textContent = message;
    locationStatus.hidden = !message;
  }

  locate.addEventListener("click", async () => {
    locate.disabled = true;
    setLocationStatus("Определяем местоположение…");
    try {
      const position = await requestPosition(telegram);
      if (!validPosition(position)) throw new Error("unavailable");
      state.origin = { lat: position.latitude, lon: position.longitude };
      locate.hidden = true;
      clearLocation.hidden = false;
      setLocationStatus("События отсортированы по расстоянию.");
      if (state.enabled) clearLocation.focus({ preventScroll: true });
      render();
    } catch (error) {
      setLocationStatus(error?.message === "denied"
        ? "Доступ к местоположению не разрешён. Список событий доступен."
        : "Местоположение недоступно. Попробуйте ещё раз.");
    } finally {
      locate.disabled = false;
    }
  });
  clearLocation.addEventListener("click", () => {
    state.origin = null;
    locate.hidden = false;
    clearLocation.hidden = true;
    setLocationStatus("");
    render();
    locate.focus({ preventScroll: true });
  });
  toggle.addEventListener("click", () => showList(body.hidden));
  map?.on("zoomend", renderMarkers);
  mobile.addEventListener("change", () => showList(!mobile.matches));
  document.addEventListener("visibilitychange", () => {
    if (state.enabled && !document.hidden) render();
    scheduleClock();
  });
  showList(!mobile.matches);
  return {
    enter(id = "") {
      state.enabled = true;
      state.group = "";
      state.selected = "";
      if (id) state.pending = id;
      show();
      if (!state.loaded || state.pending) void load();
    },
    leave() {
      state.enabled = false;
      clearTimeout(clock);
      clearTimeout(retry);
      layer?.clearLayers();
      courseLayer?.clearLayers();
    },
    refresh() {
      attempts = 0;
      void load();
    },
  };
}
