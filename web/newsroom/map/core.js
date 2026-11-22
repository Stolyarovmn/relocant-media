export const kinds = {
  water: { label: "Вода", color: "#00669f", night: "#56b5ed", icon: "💧" },
  power: { label: "Свет", color: "#6937b2", night: "#bf9aff", icon: "⚡" },
  road: { label: "Дороги", color: "#8b202e", night: "#fb808b", icon: "⛔" },
};

export const placeKinds = {
  music: { label: "Музыка", icon: "♫" },
  exhibition: { label: "Выставки", icon: "▣" },
  sport: { label: "Спорт", icon: "🏅" },
  other: { label: "События", icon: "★" },
};

const eventId = /^event_[a-f0-9]{20}$/;
const timestamp = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})$/;
const calendarDate = /^\d{4}-\d{2}-\d{2}$/;
const hour = new Intl.DateTimeFormat("ru", {
  timeZone: "Europe/Belgrade", hour: "2-digit", minute: "2-digit",
});
const day = new Intl.DateTimeFormat("ru", {
  timeZone: "Europe/Belgrade", day: "numeric", month: "short", year: "numeric",
});
const localDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Europe/Belgrade", year: "numeric", month: "2-digit", day: "2-digit",
});
const localHour = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Europe/Belgrade", hour: "2-digit", hourCycle: "h23",
});

export function belgradeDate(instant) {
  const parts = localDate.formatToParts(instant);
  return ["year", "month", "day"].map((type) => parts.find((part) => part.type === type).value).join("-");
}

export function mapTheme(preference, now = Date.now()) {
  if (preference === "dark" || preference === "light") return preference;
  const hour = Number(localHour.format(now));
  return hour >= 7 && hour < 19 ? "light" : "dark";
}

const cyrillic = {
  а: "a", б: "b", в: "v", г: "g", д: "d", ђ: "đ", е: "e", ё: "e", ж: "ž",
  з: "z", и: "i", й: "j", ј: "j", к: "k", л: "l", љ: "lj", м: "m", н: "n",
  њ: "nj", о: "o", п: "p", р: "r", с: "s", т: "t", ћ: "ć", у: "u", ф: "f",
  х: "h", ц: "c", ч: "č", џ: "dž", ш: "š", щ: "š", ы: "i", ъ: "", ь: "",
  э: "e", ю: "ju", я: "ja",
};

function latin(text) {
  return String(text ?? "").normalize("NFC").toLowerCase()
    .replace(/[а-яёђјљњћџ]/g, (letter) => cyrillic[letter] ?? letter)
    .replace(/[^a-zčćžšđ0-9]+/g, " ").trim();
}

export function normalise(text) {
  return latin(text).replace(/đ/g, "dj")
    .normalize("NFD").replace(/\p{M}/gu, "")
    .replace(/[^a-z0-9]+/g, " ").trim();
}

const streetName = (value) => normalise(value).replace(/^(ulica|ul|ulitsa) /, "");
const municipalityName = (value) => normalise(value).replace(/^(gradska opstina|opstina) /, "");
const houseName = (value) => normalise(value).replace(/ /g, "");
const exactStreet = (value) => latin(value).replace(/^(ulica|ul|ulitsa) /, "");
const exactDistrict = (value) => latin(value).replace(/^(gradska opština|opština) /, "");
const textValue = (value) => typeof value === "string" ? value.trim() : "";
const array = (value) => Array.isArray(value) ? value : [];
const coords = (lat, lon) => Number.isFinite(lat) && Number.isFinite(lon)
  && lat >= 44 && lat <= 46 && lon >= 19 && lon <= 22;

export function safeLink(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : "";
  } catch {
    return "";
  }
}

const shareApp = "https://t.me/relocant_media_belgrade_bot";

// Ссылка «Поделиться»: окно отправки Telegram с карточкой этой же карты.
export function shareLink(id, title = "", places = false) {
  if (typeof id !== "string" || !eventId.test(id)) return "";
  const target = `${shareApp}?startapp=${places ? "places_" : ""}${id}`;
  const text = typeof title === "string" ? title.trim().slice(0, 200) : "";
  return `https://t.me/share/url?url=${encodeURIComponent(target)}`
    + (text ? `&text=${encodeURIComponent(text)}` : "");
}

export function launchEvent(search, telegramParam) {
  const params = new URLSearchParams(search);
  return [params.get("event"), params.get("tgWebAppStartParam"), telegramParam]
    .find((value) => typeof value === "string" && eventId.test(value)) || "";
}

export function launchMap(search, telegramParam) {
  const params = new URLSearchParams(search);
  const browserEvent = params.get("event");
  if (params.get("mode") === "places") {
    return { mode: "places", id: eventId.test(browserEvent) ? browserEvent : "" };
  }
  if (!eventId.test(browserEvent)) {
    for (const value of [params.get("tgWebAppStartParam"), telegramParam]) {
      if (typeof value !== "string") continue;
      if (eventId.test(value)) break;
      if (value === "places") return { mode: "places", id: "" };
      if (value.startsWith("places_") && eventId.test(value.slice(7))) {
        return { mode: "places", id: value.slice(7) };
      }
    }
  }
  return { mode: "incidents", id: launchEvent(search, telegramParam) };
}

export function launchDigestEvents(search, telegramParam) {
  const params = new URLSearchParams(search);
  for (const candidate of [params.get("events"), params.get("tgWebAppStartParam"), telegramParam]) {
    if (typeof candidate !== "string") continue;
    const ids = candidate.startsWith("digest_") ? candidate.slice(7).split("-") : candidate.split("~");
    if (ids.length >= 2 && ids.length <= 4 && ids.every((id) => eventId.test(id))) {
      return [...new Set(ids)];
    }
  }
  return [];
}

function parseTime(value) {
  if (typeof value !== "string" || !timestamp.test(value)) return null;
  const result = Date.parse(value);
  return Number.isFinite(result) ? result : null;
}

function dateOnly(value) {
  if (typeof value !== "string" || !calendarDate.test(value)) return null;
  const result = Date.parse(value + "T12:00:00Z");
  if (!Number.isFinite(result) || new Date(result).toISOString().slice(0, 10) !== value) {
    return null;
  }
  return result;
}

export function agendaDays(now, length = 7) {
  const start = dateOnly(belgradeDate(now));
  return Array.from({ length }, (_, index) => new Date(start + index * 86_400_000).toISOString().slice(0, 10));
}

export function placeOnDay(place, key) {
  return !key || (belgradeDate(place.start) <= key
    && belgradeDate(Number.isFinite(place.end) ? place.end - 1 : place.start) >= key);
}

export function placeCategory(place) {
  const title = String(place.title).normalize("NFC").toLowerCase();
  if (place.courses?.length) return "sport";
  if (/выставк|изложб|экспозиц|exhibition|izložb|izlozb/.test(title)) return "exhibition";
  if (/концерт|музык|джаз|кабаре|koncert|concert|jazz|muzi[čk]|музич/.test(title)) return "music";
  if (/марафон|спорт|полумара|трка|maraton|\bsport/.test(title)) return "sport";
  return "other";
}

function readWindow(window) {
  const start = parseTime(window?.starts_at);
  const end = parseTime(window?.ends_at);
  return start !== null && end !== null && start < end ? { start, end } : null;
}

function readPlaceCourses(value) {
  if (!Array.isArray(value) || value.length > 4) return [];
  const names = new Set();
  const courses = [];
  for (const course of value) {
    const name = textValue(course?.name);
    const key = name.toLowerCase();
    const points = course?.coordinates;
    if (!name || name.length > 80 || names.has(key)
        || !Array.isArray(points) || points.length < 2 || points.length > 4096) return [];
    const latLngs = [];
    for (const point of points) {
      if (!Array.isArray(point) || point.length !== 2) return [];
      const [lon, lat] = point;
      if (!Number.isFinite(lon) || !Number.isFinite(lat)
          || lon < 19.8 || lon > 21.1 || lat < 44.2 || lat > 45.2) return [];
      latLngs.push([lat, lon]);
    }
    if (latLngs.every(([lat, lon]) => lat === latLngs[0][0] && lon === latLngs[0][1])) return [];
    names.add(key);
    courses.push({ name, latLngs });
  }
  return courses;
}

export function readPlaces(document) {
  if (document?.version !== 1 || !Array.isArray(document.events)) {
    throw new Error("Не удалось прочитать афишу");
  }
  const seen = new Set();
  return [...document.events, ...array(document.sponsored_places)].flatMap((item) => {
    if (typeof item?.id !== "string" || !eventId.test(item.id) || seen.has(item.id)
        || !["event", "sponsored_place"].includes(item.kind)
        || item.timezone !== "Europe/Belgrade") return [];
    const title = textValue(item.title);
    const source = safeLink(item.source_url);
    const schedule = safeLink(item.schedule_source_url);
    const post = safeLink(item.post_url);
    const venue = textValue(item.venue?.name);
    const address = textValue(item.venue?.address);
    const lat = item.venue?.latitude;
    const lon = item.venue?.longitude;
    const start = parseTime(item.starts_at);
    const end = parseTime(item.ends_at);
    const expires = parseTime(item.expires_at);
    if (!title || !source || !venue || !coords(lat, lon) || start === null
        || expires === null || expires <= start
        || (item.ends_at != null && (end === null || end <= start || end > expires))) return [];
    seen.add(item.id);
    const paidPlace = item.kind === "sponsored_place";
    if (paidPlace && (!post || end !== null)) return [];
    const group = eventId.test(item.group_id) ? item.group_id : "";
    return [{ id: item.id, group, title, source, schedule: schedule === source ? "" : schedule,
      post, venue, address, visitorHours: textValue(item.visitor_hours),
      scheduleNote: textValue(item.schedule_note), venueNote: textValue(item.venue_note), lat, lon,
      courses: paidPlace ? [] : readPlaceCourses(item.courses),
      paidPlace, sponsored: paidPlace || item.sponsored === true,
      archived: item.archived === true, published: parseTime(item.published_at), start, end, expires }];
  });
}

export function placeStatus(place, now) {
  if (place.archived || now >= place.expires || (place.end !== null && now >= place.end)) return "closed";
  return now < place.start ? "planned" : "active";
}

export function launchPlaces(entries, id) {
  if (!eventId.test(id)) return [];
  const exact = entries.find((place) => place.id === id);
  return exact ? [exact] : entries.filter((place) => place.group === id);
}

export function placeOptionLabel(place) {
  return `${place.venue} · ${day.format(place.start)}, ${hour.format(place.start)}`;
}

export function placeCountdown(place, now) {
  if (place.paidPlace) return "";
  if (place.scheduleNote) return "Дата уточняется";
  const status = placeStatus(place, now);
  if (status === "closed") return place.end === null ? "Прошедшая дата" : "Завершилось";
  if (status === "active" && place.end === null) return "Началось";
  return incidentCountdown({ ...place, windows: [] }, now);
}

export function straightLineDistance(origin, destination) {
  const rad = Math.PI / 180;
  const lat = (destination.lat - origin.lat) * rad;
  const lon = (destination.lon - origin.lon) * rad;
  const chord = Math.sin(lat / 2) ** 2
    + Math.cos(origin.lat * rad) * Math.cos(destination.lat * rad) * Math.sin(lon / 2) ** 2;
  return 12_742_000 * Math.asin(Math.min(1, Math.sqrt(chord)));
}

function readAddress(item) {
  const street = textValue(item?.street);
  const number = textValue(item?.number);
  return street && number ? { street, number, municipality: textValue(item.municipality),
    window: readWindow(item) } : null;
}

function readArea(item, kind) {
  const scopes = kind === "road" ? ["road", "building", "settlement"] : ["building", "settlement"];
  if (!item || !scopes.includes(item.scope)) return null;
  const validRing = (ring) => Array.isArray(ring) && ring.length >= 4 && ring.length <= 1000
    && ring.every((pair) => Array.isArray(pair) && pair.length === 2 && coords(...pair))
    && ring[0][0] === ring.at(-1)[0] && ring[0][1] === ring.at(-1)[1];
  if (!Array.isArray(item.rings) || !item.rings.length || item.rings.length > 16
      || !item.rings.every(validRing)) return null;
  return { scope: item.scope, rings: item.rings, label: textValue(item.label),
    window: readWindow(item), address: readAddress(item.address) };
}

export function readIncidents(document) {
  if (document?.version !== 1 || !Array.isArray(document.incidents)) {
    throw new Error("Не удалось прочитать список объявлений");
  }
  const seen = new Set();
  return document.incidents.flatMap((record) => {
    if (!record || typeof record.id !== "string" || !eventId.test(record.id)
        || !Object.hasOwn(kinds, record.kind) || seen.has(record.id)) return [];
    const title = textValue(record.title);
    const source = safeLink(record.source_url);
    if (!title || !source) return [];
    seen.add(record.id);
    return [{
      id: record.id,
      kind: record.kind,
      title,
      archived: record.archived === true,
      reportedScope: textValue(record.reported_scope),
      source,
      officialMap: safeLink(record.source_map_url),
      published: parseTime(record.published_at),
      start: parseTime(record.starts_at),
      end: parseTime(record.ends_at),
      startDay: dateOnly(record.starts_on),
      endDay: dateOnly(record.ends_on),
      windows: array(record.windows).map(readWindow).filter(Boolean)
        .sort((a, b) => a.start - b.start),
      points: array(record.points).flatMap((point) => coords(point?.lat, point?.lon)
        ? [{ lat: point.lat, lon: point.lon, label: textValue(point.label),
          address: readAddress(point.address), fromRegister: point.coordinate_source === "rgz",
          approximate: point.approximate === true }] : []),
      lines: array(record.lines).flatMap((line) => Array.isArray(line?.coordinates)
        && line.coordinates.length >= 2
        && line.coordinates.every((pair) => Array.isArray(pair) && coords(pair[0], pair[1]))
        ? [{ coordinates: line.coordinates.map(([lat, lon]) => [lat, lon]),
          label: textValue(line.label), window: readWindow(line), boundary: line.boundary === true }] : []),
      areas: array(record.areas).map((area) => readArea(area, record.kind)).filter(Boolean),
      addresses: array(record.addresses).map(readAddress).filter(Boolean),
      ranges: array(record.ranges).map(readAddress).filter(Boolean),
      waterDistricts: array(record.water_districts).flatMap((item) => {
        const municipality = textValue(item?.municipality);
        const window = readWindow(item);
        return municipality && window ? [{ municipality, window,
          boundary: array(item.boundary).map(textValue).filter(Boolean),
          wholeStreets: array(item.whole_streets).map(textValue).filter(Boolean) }] : [];
      }),
      settlements: array(record.settlements).flatMap((item) => textValue(item?.name)
        ? [{ name: textValue(item.name), municipality: textValue(item.municipality),
          parent: textValue(item.parent), window: readWindow(item) }] : []),
      segments: array(record.segments).flatMap((item) => {
        const street = textValue(item?.street);
        if (!street) return [];
        return [{ street, start: textValue(item.start), end: textValue(item.end),
          municipality: textValue(item.municipality) }];
      }),
    }];
  });
}

export function incidentStatus(incident, now) {
  if (incident.archived) return "closed";
  if (incident.end !== null && now >= incident.end) return "closed";
  if (incident.windows.length) {
    if (incident.windows.some(({ start, end }) => start <= now && now < end)) return "active";
    if (now < incident.windows[0].start) return "planned";
    if (incident.windows.some(({ start }) => start > now)) return "between";
    if (incident.end === null) return "closed";
    return "unknown";
  }
  if (incident.start !== null && now < incident.start) return "planned";
  if (incident.start !== null && incident.end !== null) return "active";
  return "unknown";
}

export function unexpired(window, now, archive = false) {
  return archive || !window || now < window.end;
}

export function incidentInPeriod(incident, now, hours = null) {
  if (hours === null) return incident;
  if (incident.archived) return null;
  const until = now + hours * 3_600_000;
  const overlaps = ({ start, end }) => Number.isFinite(start) && start <= until
    && (end === null ? start > now : end > now);
  const windows = incident.windows.filter(overlaps);
  if (incident.windows.length ? !windows.length : !overlaps(incident)) return null;
  const matches = (window) => !window || overlaps(window);
  const inPeriod = (entries) => entries.filter(({ window }) => matches(window));
  return {
    ...incident,
    windows,
    points: incident.points.filter(({ address }) => matches(address?.window)),
    lines: inPeriod(incident.lines),
    areas: incident.areas.filter(({ window, address }) => matches(window || address?.window)),
    addresses: inPeriod(incident.addresses),
    ranges: inPeriod(incident.ranges),
    waterDistricts: inPeriod(incident.waterDistricts),
    settlements: inPeriod(incident.settlements),
  };
}

export function visibleMapLines(incident, now, archive = false) {
  const fallback = incident.end === null ? null : incident;
  return incident.lines.filter(({ window }) => unexpired(window || fallback, now, archive));
}

export function nextTransition(incidents, now, hours = 0) {
  let closest = Infinity;
  const consider = (value) => {
    if (value !== null && value > now && value < closest) closest = value;
  };
  const considerStart = (value) => {
    consider(value);
    if (hours && value !== null) consider(value - hours * 3_600_000);
  };
  for (const incident of incidents) {
    if (incident.archived) continue;
    considerStart(incident.start);
    consider(incident.end);
    for (const window of incident.windows) {
      considerStart(window.start);
      consider(window.end);
    }
    for (const { window } of incident.lines) {
      if (!window) continue;
      considerStart(window.start);
      consider(window.end);
    }
  }
  return closest;
}

export function incidentCountdown(incident, now) {
  if (incident.archived || incidentStatus(incident, now) === "closed") return "";
  const windows = incident.windows.length ? incident.windows : [{ start: incident.start, end: incident.end }];
  const active = windows.filter(({ start, end }) => start !== null && end !== null
    && start <= now && end > now);
  const future = windows.filter(({ start }) => start !== null && start > now);
  const target = active.length ? Math.min(...active.map(({ end }) => end))
    : future.length ? Math.min(...future.map(({ start }) => start)) : incident.end;
  if (target === null || target <= now) return "";
  const minutes = Math.floor((target - now) / 60000);
  const days = Math.floor(minutes / 1440);
  const hours = Math.floor(minutes / 60) % 24;
  const rest = minutes % 60;
  const duration = [days ? `${days} д` : "", hours ? `${hours} ч` : "",
    !days && rest ? `${rest} мин` : ""].filter(Boolean).join(" ") || "меньше минуты";
  const label = future.length && !active.length ? "До начала"
    : active.length + future.length > 1 ? "До ближайшего окончания" : "До окончания";
  return `${label} · ${duration}`;
}

function span(start, end) {
  const sameDay = day.format(start) === day.format(end);
  return sameDay
    ? `${day.format(start)} · ${hour.format(start)}–${hour.format(end)}`
    : `${day.format(start)}, ${hour.format(start)} — ${day.format(end)}, ${hour.format(end)}`;
}

export function addressWindowText(window) {
  return window ? span(window.start, window.end) : "";
}

export function incidentTimes(incident) {
  if (incident.startDay !== null && incident.endDay !== null) {
    return [incident.startDay === incident.endDay
      ? day.format(incident.startDay)
      : `${day.format(incident.startDay)} — ${day.format(incident.endDay)}`];
  }
  if (incident.windows.length) return incident.windows.map(addressWindowText);
  if (incident.start !== null && incident.end !== null) return [span(incident.start, incident.end)];
  if (incident.start !== null) {
    return [`С ${day.format(incident.start)}, ${hour.format(incident.start)}`];
  }
  if (incident.end !== null) return [`До ${day.format(incident.end)}, ${hour.format(incident.end)}`];
  return incident.published === null ? [] : [`Опубликовано ${day.format(incident.published)}`];
}

export function updateStamp(value) {
  const date = parseTime(value);
  if (date === null) return "";
  return `${day.format(date)}, ${hour.format(date)}`;
}

function addressKey(address) {
  const district = exactDistrict(address.municipality);
  const street = exactStreet(address.street);
  const number = exactHouse(address.number);
  return district && street && number ? `${street}|${number}|${district}` : "";
}

function exactHouse(value) {
  const raw = textValue(value);
  if (!/^[0-9]+(?:\s*\p{L}+)?$/u.test(raw)) return "";
  const number = latin(raw).replace(/ /g, "");
  return /^[0-9]+[a-zčćžšđ]*$/.test(number) ? number : "";
}

const streetKey = (address) => `${exactStreet(address.street)}|${exactDistrict(address.municipality)}`;

function cachedForms(cache, value, toExact, toPlain) {
  let forms = cache.get(value);
  if (!forms) {
    forms = { exact: toExact(value), plain: toPlain(value) };
    cache.set(value, forms);
  }
  return forms;
}

export function buildAddressIndex(document) {
  if (document?.version !== 1 || !Array.isArray(document.addresses)) {
    throw new Error("Не удалось прочитать индекс адресов");
  }
  const rows = [];
  const exact = new Map();
  const streets = new Map();
  const districts = new Set();
  const byNumber = new Map();
  const streetForms = new Map();
  const districtForms = new Map();
  const numberForms = new Map();
  for (const row of document.addresses) {
    if (!Array.isArray(row) || !coords(row[3], row[4])) continue;
    const [street, number, municipality, lat, lon] = row;
    if (!textValue(street) || !textValue(number)) continue;
    const value = { street, number, municipality: textValue(municipality), lat, lon };
    const streetLabel = cachedForms(streetForms, street, exactStreet, streetName);
    const houseLabel = cachedForms(numberForms, number, exactHouse, houseName);
    const districtLabel = cachedForms(
      districtForms, value.municipality, exactDistrict, municipalityName,
    );
    const key = streetLabel.exact && houseLabel.exact && districtLabel.exact
      ? `${streetLabel.exact}|${houseLabel.exact}|${districtLabel.exact}` : "";
    const previous = exact.get(key);
    if (key && !exact.has(key)) {
      exact.set(key, value);
    } else if (previous && (previous.lat !== lat || previous.lon !== lon)) {
      exact.set(key, null);
    } else if (previous) {
      continue;
    }
    const plainStreet = streetLabel.plain;
    const plainNumber = houseLabel.plain;
    const plainDistrict = districtLabel.plain;
    const indexed = { value, plainStreet, plainNumber,
      search: `${plainStreet} ${plainNumber} ${plainDistrict}`.trim() };
    rows.push(indexed);
    const sameNumber = byNumber.get(plainNumber);
    if (sameNumber) sameNumber.push(indexed);
    else byNumber.set(plainNumber, [indexed]);
    if (plainDistrict) {
      districts.add(plainDistrict);
      const sKey = `${streetLabel.exact}|${districtLabel.exact}`;
      const bounds = streets.get(sKey);
      if (bounds) {
        bounds[0] = Math.min(bounds[0], lat);
        bounds[1] = Math.min(bounds[1], lon);
        bounds[2] = Math.max(bounds[2], lat);
        bounds[3] = Math.max(bounds[3], lon);
      } else {
        streets.set(sKey, [lat, lon, lat, lon]);
      }
    }
  }
  if (!rows.length) throw new Error("Индекс адресов пуст");
  return { rows, exact, streets, districts, byNumber };
}

export function searchAddresses(index, query, exactNumber = false) {
  const value = streetName(query);
  if (value.length < 3) return [];
  const words = value.split(" ");
  const housePosition = words.findIndex((word, position) => position > 0
    && /^\d+[a-z]*$/.test(word)
    && (position === words.length - 1 || index.districts.has(words.slice(position + 1).join(" "))));
  const requestedNumber = housePosition >= 0 ? words[housePosition] : "";
  const requestedStreet = requestedNumber ? words.slice(0, housePosition).join(" ") : value;
  const candidates = exactNumber && requestedNumber
    ? index.byNumber.get(requestedNumber) || [] : index.rows;
  const results = [];
  for (const row of candidates) {
    if (!words.every((word) => row.search.startsWith(word) || row.search.includes(` ${word}`))) {
      continue;
    }
    let score = row.plainStreet === requestedStreet ? 8
      : row.plainStreet.startsWith(requestedStreet) ? 4 : 0;
    if (requestedNumber && row.plainNumber === requestedNumber) score += 8;
    if (row.search.startsWith(value)) score += 2;
    results.push({ score, value: row.value });
  }
  return results.sort((a, b) => b.score - a.score).slice(0, 7).map(({ value }) => value);
}

export function addressHits(incident, selected) {
  const key = selected && addressKey(selected);
  if (!key) return [];
  const matches = new Set(incident.addresses.filter((address) => addressKey(address) === key));
  const near = (lat, lon) => Math.abs(lat - selected.lat) < .00012
    && Math.abs(lon - selected.lon) < .00016;
  const scoped = (address) => address && addressKey({ ...address,
    municipality: address.municipality.split(/,\s*naselje\s+/i)[0] }) === key;
  const confirmed = [...incident.points.filter((point) => near(point.lat, point.lon)),
    ...incident.areas.filter((area) => area.rings[0].some(([lat, lon]) => near(lat, lon)))];
  for (const { address } of confirmed) {
    if (!scoped(address)) continue;
    for (const entry of incident.addresses) {
      if (addressKey(entry) === addressKey(address)) matches.add(entry);
    }
  }
  return [...matches];
}

export function exactMapPoints(index, incident, now = -Infinity, archive = false) {
  const points = incident.points.filter((point) => unexpired(point.address?.window, now, archive));
  if (!index || incident.kind !== "power") return points;
  const visibleAreas = incident.areas.filter((area) => unexpired(area.window, now, archive));
  const covered = new Set([...points, ...visibleAreas]
    .filter(({ address }) => address).map(({ address }) => addressKey(address)));
  for (const item of incident.addresses) {
    const key = addressKey(item);
    if (!unexpired(item.window, now, archive) || covered.has(key)) continue;
    const match = index.exact.get(key);
    if (!match || points.some(({ lat, lon }) => Math.abs(lat - match.lat) < .000005
      && Math.abs(lon - match.lon) < .000005)) continue;
    points.push({ lat: match.lat, lon: match.lon, label: `${item.street}, ${item.number}`,
      address: item });
  }
  return points;
}

function insideRing(point, ring) {
  let inside = false;
  for (let i = 1; i < ring.length; i += 1) {
    const a = ring[i - 1];
    const b = ring[i];
    if ((a.y > point.y) !== (b.y > point.y)
        && point.x < a.x + (b.x - a.x) * (point.y - a.y) / (b.y - a.y)) inside = !inside;
  }
  return inside;
}

function nearEdge(point, path, radius) {
  for (let i = 1; i < path.length; i += 1) {
    const a = path[i - 1];
    const b = path[i];
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const span = dx * dx + dy * dy;
    const ratio = span ? ((point.x - a.x) * dx + (point.y - a.y) * dy) / span : 0;
    const offset = Math.max(0, Math.min(1, ratio));
    if (Math.hypot(point.x - a.x - offset * dx, point.y - a.y - offset * dy) <= radius) {
      return true;
    }
  }
  return false;
}

function projectedShape(paths, source, area, project) {
  const parts = paths.map((path) => path.map(project));
  const bounds = [Infinity, Infinity, -Infinity, -Infinity];
  for (const part of parts) {
    for (const { x, y } of part) {
      bounds[0] = Math.min(bounds[0], x);
      bounds[1] = Math.min(bounds[1], y);
      bounds[2] = Math.max(bounds[2], x);
      bounds[3] = Math.max(bounds[3], y);
    }
  }
  const path = paths[0];
  const middle = Math.floor((path.length - 1) / 2);
  const a = path[middle];
  const b = path[middle + 1];
  return { parts, bounds, area, marker: {
    lat: (a[0] + b[0]) / 2, lon: (a[1] + b[1]) / 2, label: source.label,
    address: source.address, window: source.window,
  } };
}

function coversPoint(shape, point, radius) {
  const [left, top, right, bottom] = shape.bounds;
  if (point.x < left - radius || point.x > right + radius
      || point.y < top - radius || point.y > bottom + radius) return false;
  const [exterior, ...holes] = shape.parts;
  if (shape.area && insideRing(point, exterior) && !holes.some((hole) => insideRing(point, hole))) {
    return true;
  }
  return shape.parts.some((part) => nearEdge(point, part, radius));
}

function representsPin(shape, pin, pixel) {
  const pinWindow = pin.window || pin.address?.window;
  const shapeWindow = shape.marker.window || shape.marker.address?.window;
  if (pinWindow && shapeWindow
      && (pinWindow.start !== shapeWindow.start || pinWindow.end !== shapeWindow.end)) {
    return false;
  }
  if (pin.address && shape.marker.address) {
    return addressKey(pin.address) === addressKey(shape.marker.address);
  }
  return coversPoint(shape, pixel, pin.address ? 0 : shape.area ? 8 : 11);
}

export function pointsAtScale(points, lines, areas, zoom, project) {
  const shapes = [
    ...lines.map((line) => projectedShape([line.coordinates], line, false, project)),
    ...areas.map((area) => projectedShape(area.rings, area, true, project)),
  ];
  if (!shapes.length) return points;
  const pins = points.map((pin) => ({ pin, pixel: project([pin.lat, pin.lon]) }));
  if (zoom >= 16) {
    return pins.filter(({ pin, pixel }) => !shapes.some((shape) =>
      representsPin(shape, pin, pixel)))
      .map(({ pin }) => pin);
  }
  const represented = new Set(points.flatMap(({ address }) => address ? [addressKey(address)] : []));
  const overview = shapes.filter((shape) => {
    const key = shape.marker.address && addressKey(shape.marker.address);
    return !(key && represented.has(key)) && !pins.some(({ pixel }) => coversPoint(shape, pixel, .1));
  });
  return [...points, ...overview.map(({ marker }) => marker)];
}

export function clusterMapPoints(points, project, radius) {
  const buckets = new Map();
  const groups = [];
  for (const point of points) {
    const pixel = project([point.lat, point.lon]);
    const x = Math.floor(pixel.x / radius);
    const y = Math.floor(pixel.y / radius);
    let target;
    let nearest = radius;
    for (let dx = -1; dx <= 1; dx += 1) {
      for (let dy = -1; dy <= 1; dy += 1) {
        for (const candidate of buckets.get(`${x + dx}:${y + dy}`) || []) {
          const distance = Math.hypot(candidate.pixel.x - pixel.x, candidate.pixel.y - pixel.y);
          if (distance < nearest) {
            target = candidate;
            nearest = distance;
          }
        }
      }
    }
    if (target) {
      target.points.push(point);
    } else {
      const group = { pixel, points: [point] };
      const key = `${x}:${y}`;
      const bucket = buckets.get(key);
      if (bucket) bucket.push(group);
      else buckets.set(key, [group]);
      groups.push(group);
    }
  }
  return groups.map(({ points }) => points);
}

export function streetBounds(index, incident) {
  if (!index || incident.segments.length !== 1) return null;
  const segment = incident.segments[0];
  if (!exactDistrict(segment.municipality)) return null;
  const bounds = index.streets.get(streetKey(segment));
  if (!bounds || bounds[2] - bounds[0] > .035 || bounds[3] - bounds[1] > .048) return null;
  return bounds;
}
