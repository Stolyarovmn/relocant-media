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

export function timelineStepAt(origin, instant) {
  if (!Number.isFinite(instant) || instant <= origin) return 0;
  const step = 1 + (instant - timelineInstant(origin, 1)) / halfHour;
  return Number.isInteger(step) && step >= 1 && step <= timelineSteps ? step : 0;
}

export function timelineLabel(instant, current) {
  return current ? "Сейчас" : hourLabel.format(instant);
}

export function timelineBands(incidents, origin, end) {
  const byKind = new Map();
  for (const incident of incidents) {
    if (incident.archived || incident.roadSchedule === "weekly_day") continue;
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

export function keepsBoundaryInHeat(area) {
  return area.scope === "settlement";
}

// Leaflet's projected world pixels keep cell membership fixed across screen sizes at a given zoom.
const HEAT_CELL_SIZE = 76;

function visitHeatSegment(start, finish, visit) {
  let column = Math.floor(start.x / HEAT_CELL_SIZE);
  let row = Math.floor(start.y / HEAT_CELL_SIZE);
  const endColumn = Math.floor(finish.x / HEAT_CELL_SIZE);
  const endRow = Math.floor(finish.y / HEAT_CELL_SIZE);
  const dx = finish.x - start.x;
  const dy = finish.y - start.y;
  const stepX = Math.sign(dx);
  const stepY = Math.sign(dy);
  visit(column, row);
  while (column !== endColumn || row !== endRow) {
    const nextX = column === endColumn ? Infinity
      : ((column + (stepX > 0 ? 1 : 0)) * HEAT_CELL_SIZE - start.x) / dx;
    const nextY = row === endRow ? Infinity
      : ((row + (stepY > 0 ? 1 : 0)) * HEAT_CELL_SIZE - start.y) / dy;
    // At a corner, advance both axes so the two cells only touched there are not counted.
    if (nextX <= nextY) column += stepX;
    if (nextY <= nextX) row += stepY;
    visit(column, row);
  }
}

export function heatCells(points, project, lines = []) {
  const cells = new Map();
  function add(column, row, incident) {
    const key = `${column}:${row}`;
    if (!cells.has(key)) {
      cells.set(key, {
        x: (column + .5) * HEAT_CELL_SIZE,
        y: (row + .5) * HEAT_CELL_SIZE,
        incidents: new Map(),
      });
    }
    cells.get(key).incidents.set(incident.id, incident);
  }
  for (const { lat, lon, incident, approximate } of points) {
    if (approximate) continue;
    const { x, y } = project([lat, lon]);
    add(Math.floor(x / HEAT_CELL_SIZE), Math.floor(y / HEAT_CELL_SIZE), incident);
  }
  for (const { coordinates, incident } of lines) {
    const projected = coordinates.map(project);
    for (let index = 1; index < projected.length; index += 1) {
      visitHeatSegment(projected[index - 1], projected[index], (column, row) => {
        add(column, row, incident);
      });
    }
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
