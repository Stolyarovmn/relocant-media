export function layerView(value) {
  return value === "corridor" || value === "heat" ? value : "detail";
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

export function corridorCaps(coordinates, project) {
  const first = coordinates[0];
  const last = coordinates.at(-1);
  const start = project(first);
  const end = project(last);
  return Math.hypot(end.x - start.x, end.y - start.y) >= 22 ? [first, last] : [];
}
