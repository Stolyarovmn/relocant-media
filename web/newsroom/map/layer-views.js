export function layerView(value) {
  return ["corridor", "heat", "timeline"].includes(value) ? value : "detail";
}

export const timelineSteps = 48;
const halfHour = 30 * 60_000;
const hourLabel = new Intl.DateTimeFormat("ru", {
  timeZone: "Europe/Belgrade", day: "numeric", month: "short",
  hour: "2-digit", minute: "2-digit",
});

export function timelineInstant(origin, step) {
  if (step === 0) return origin;
  const next = Math.floor(origin / halfHour) * halfHour + halfHour;
  return next + (Math.min(timelineSteps, Math.max(1, step)) - 1) * halfHour;
}

export function timelineLabel(instant, current) {
  return current ? "Сейчас" : hourLabel.format(instant);
}

export function timelineBands(incidents, origin, end) {
  const byKind = new Map();
  for (const incident of incidents) {
    if (incident.archived) continue;
    for (const span of incident.windows.length ? incident.windows : [incident]) {
      if (!Number.isFinite(span.start) || !Number.isFinite(span.end)) continue;
      const start = Math.max(origin, span.start);
      const finish = Math.min(end, span.end);
      if (start >= finish) continue;
      if (!byKind.has(incident.kind)) byKind.set(incident.kind, []);
      byKind.get(incident.kind).push([start, finish]);
    }
  }
  for (const [kind, spans] of byKind) {
    const bands = [];
    for (const [start, end] of spans.sort((a, b) => a[0] - b[0])) {
      const previous = bands.at(-1);
      if (previous && start <= previous[1]) previous[1] = Math.max(previous[1], end);
      else bands.push([start, end]);
    }
    byKind.set(kind, bands);
  }
  return byKind;
}

export function heatOverview(view, zoom, preciseRoute) {
  return view === "heat" && zoom < 15 && !preciseRoute;
}

export function heatCells(points, project, size) {
  const cells = new Map();
  for (const { lat, lon, incident, approximate } of points) {
    if (approximate) continue;
    const { x, y } = project([lat, lon]);
    const column = Math.floor(x / size);
    const row = Math.floor(y / size);
    const key = `${column}:${row}`;
    if (!cells.has(key)) {
      cells.set(key, { x: (column + .5) * size, y: (row + .5) * size, incidents: new Map() });
    }
    cells.get(key).incidents.set(incident.id, incident);
  }
  return [...cells.values()].map(({ x, y, incidents }) => ({
    x, y, incidents: [...incidents.values()],
  }));
}

export function heatComposition(incidents, kindOrder) {
  const counts = new Map();
  for (const { kind } of incidents) counts.set(kind, (counts.get(kind) || 0) + 1);
  let seen = 0;
  return kindOrder.filter((kind) => counts.has(kind)).map((kind) => {
    const count = counts.get(kind);
    const start = 100 * seen / incidents.length;
    seen += count;
    return { kind, count, start, end: 100 * seen / incidents.length };
  });
}

export function corridorCaps(coordinates, project) {
  const first = coordinates[0];
  const last = coordinates.at(-1);
  const start = project(first);
  const end = project(last);
  return Math.hypot(end.x - start.x, end.y - start.y) >= 22 ? [first, last] : [];
}
