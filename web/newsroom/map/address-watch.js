import { addressHits, incidentStatus } from "./core.js?v=__MAP_REVISION__";

const storageKey = "relocant-map-saved-address-v1";
const roadRadius = 120;

function validAddress(value) {
  return [value?.street, value?.number, value?.municipality].every((label) =>
    typeof label === "string" && label.trim() && label.length <= 160)
    && Number.isFinite(value.lat) && Number.isFinite(value.lon)
    && value.lat >= 44.22 && value.lat <= 45.23 && value.lon >= 19.65 && value.lon <= 21.35;
}

const identity = (address) => JSON.stringify([
  address.street, address.number, address.municipality, address.lat, address.lon,
]);

function inRing({ lat, lon }, ring) {
  let inside = false;
  for (let i = 1; i < ring.length; i += 1) {
    const [a, x] = ring[i - 1];
    const [b, y] = ring[i];
    if ((a > lat) !== (b > lat) && lon < x + (y - x) * (lat - a) / (b - a)) inside = !inside;
  }
  return inside;
}

function nearRoad({ lat, lon }, coordinates) {
  const longitudeScale = 111_320 * Math.cos(lat * Math.PI / 180);
  const meters = ([y, x]) => [(x - lon) * longitudeScale, (y - lat) * 111_320];
  let [x, y] = meters(coordinates[0]);
  for (const point of coordinates.slice(1)) {
    const [nextX, nextY] = meters(point);
    const dx = nextX - x;
    const dy = nextY - y;
    const squared = dx * dx + dy * dy;
    const fraction = squared ? Math.max(0, Math.min(1, -(x * dx + y * dy) / squared)) : 0;
    if (Math.hypot(x + fraction * dx, y + fraction * dy) <= roadRadius) return true;
    [x, y] = [nextX, nextY];
  }
  return false;
}

export function watchedIncidents(address, incidents, now) {
  return incidents.flatMap((incident) => {
    if (incidentStatus(incident, now) === "closed") return [];
    const areas = incident.areas.filter(({ scope, rings }) =>
      scope !== "road" && inRing(address, rings[0])
        && !rings.slice(1).some((ring) => inRing(address, ring)));
    const houses = addressHits(incident, address);
    const roads = incident.kind === "road"
      ? incident.lines.filter((line) => !line.boundary && nearRoad(address, line.coordinates)) : [];
    const locations = [...houses, ...areas, ...roads];
    if (!locations.length) return [];
    const times = locations.flatMap(({ window }) => window ? [window] : []);
    if (times.length === locations.length && times.every(({ end }) => end <= now)) return [];
    const fallback = incident.windows.length === 1 ? incident.windows
      : !incident.windows.length && incident.start !== null && incident.end !== null
      ? [{ start: incident.start, end: incident.end }] : [];
    const windows = [...new Map((times.length ? times : fallback)
      .map((window) => [`${window.start}:${window.end}`, window])).values()]
      .sort((a, b) => a.start - b.start || a.end - b.end);
    const nearby = !houses.length && !areas.length;
    const signature = JSON.stringify([incident.title, incident.kind, nearby,
      windows.map(({ start, end }) => [start, end])]);
    return [{ incident, nearby, windows, signature }];
  });
}

function readSaved(storage) {
  try {
    const saved = JSON.parse(storage().getItem(storageKey));
    if (!validAddress(saved?.address)) return null;
    const baseline = saved.baseline;
    return { address: saved.address,
      baseline: Number.isFinite(baseline?.stamp) && Array.isArray(baseline?.items)
        && baseline.items.every((entry) => Array.isArray(entry) && entry.length === 2
          && entry.every((value) => typeof value === "string")) ? baseline : null };
  } catch {
    return null;
  }
}

export function addressWatch(storage = () => localStorage) {
  let saved = readSaved(storage);
  let baseline = saved?.baseline ?? null;
  function write(record) {
    try {
      if (record) storage().setItem(storageKey, JSON.stringify(record));
      else storage().removeItem(storageKey);
      return true;
    } catch {
      return false;
    }
  }

  return {
    get address() { return saved?.address ?? null; },
    isSaved(address) { return Boolean(saved && address && identity(saved.address) === identity(address)); },
    save(value) {
      if (!validAddress(value)) return false;
      const { street, number, municipality, lat, lon } = value;
      const record = { address: { street, number, municipality, lat, lon }, baseline: null };
      if (!write(record)) return false;
      saved = record;
      baseline = null;
      return true;
    },
    remove() {
      if (!write(null)) return false;
      saved = null;
      baseline = null;
      return true;
    },
    changes(items, stamp) {
      if (!baseline || !Number.isFinite(stamp) || stamp <= baseline.stamp) return new Map();
      const previous = new Map(baseline.items);
      return new Map(items.flatMap(({ incident, signature }) => {
        const before = previous.get(incident.id);
        return before === signature ? [] : [[incident.id, before === undefined ? "new" : "updated"]];
      }));
    },
    observe(items, stamp) {
      if (!saved || !Number.isFinite(stamp)) return;
      const current = readSaved(storage);
      if (!current || identity(current.address) !== identity(saved.address)
          || stamp <= (current.baseline?.stamp ?? -Infinity)) return;
      const next = { stamp, items: items.map(({ incident, signature }) => [incident.id, signature]) };
      if (write({ address: saved.address, baseline: next })) baseline ??= next;
    },
  };
}
