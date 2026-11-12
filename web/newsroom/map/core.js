export const kinds = {
  water: { label: "Вода", color: "#175a86" },
  power: { label: "Свет", color: "#815600" },
  road: { label: "Дороги", color: "#9e422f" },
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

export function launchEvent(search, telegramParam) {
  const params = new URLSearchParams(search);
  return [params.get("event"), params.get("tgWebAppStartParam"), telegramParam]
    .find((value) => typeof value === "string" && eventId.test(value)) || "";
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

function readWindow(window) {
  const start = parseTime(window?.starts_at);
  const end = parseTime(window?.ends_at);
  return start !== null && end !== null && start < end ? { start, end } : null;
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
        ? [{ lat: point.lat, lon: point.lon, label: textValue(point.label) }] : []),
      lines: array(record.lines).flatMap((line) => Array.isArray(line?.coordinates)
        && line.coordinates.length >= 2
        && line.coordinates.every((pair) => Array.isArray(pair) && coords(pair[0], pair[1]))
        ? [{ coordinates: line.coordinates.map(([lat, lon]) => [lat, lon]),
          label: textValue(line.label) }] : []),
      addresses: array(record.addresses).flatMap((item) => {
        const street = textValue(item?.street);
        const number = textValue(item?.number);
        if (!street || !number) return [];
        return [{ street, number, municipality: textValue(item.municipality),
          window: readWindow(item) }];
      }),
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
  return key ? incident.addresses.filter((address) => addressKey(address) === key) : [];
}

export function exactMapPoints(index, incident) {
  const points = [...incident.points];
  if (!index) return points;
  for (const item of incident.addresses) {
    const match = index.exact.get(addressKey(item));
    if (!match || points.some(({ lat, lon }) => Math.abs(lat - match.lat) < .000005
      && Math.abs(lon - match.lon) < .000005)) continue;
    points.push({ lat: match.lat, lon: match.lon, label: `${item.street}, ${item.number}` });
  }
  return points;
}

export function streetBounds(index, incident) {
  if (!index || incident.segments.length !== 1) return null;
  const segment = incident.segments[0];
  if (!exactDistrict(segment.municipality)) return null;
  const bounds = index.streets.get(streetKey(segment));
  if (!bounds || bounds[2] - bounds[0] > .035 || bounds[3] - bounds[1] > .048) return null;
  return bounds;
}
