const cacheName = "relocant-map-public-feeds-v1";
const timestampSkew = 60_000;

export function snapshotRefreshDue(lastCheck, now = Date.now()) {
  return !lastCheck || now < lastCheck || now - lastCheck >= 60_000;
}

async function openCache(storage) {
  try {
    return await (storage ?? globalThis.caches)?.open(cacheName);
  } catch {
    return undefined;
  }
}

function snapshot(response, data, decode) {
  const generatedAt = typeof data?.generated_at === "string" ? data.generated_at : "";
  const timestamp = Date.parse(generatedAt);
  // Prefer the host clock because the phone can be set to the wrong date.
  const serverNow = Date.parse(response.headers.get("date") ?? "");
  const now = Number.isFinite(serverNow) ? serverNow : Date.now();
  if (timestamp > now + timestampSkew) throw new Error("Дата снимка карты в будущем");
  return {
    entries: decode(data),
    generatedAt,
    timestamp,
  };
}

async function savedSnapshot(cache, url, decode) {
  try {
    const response = await (await cache)?.match(url);
    if (!response) return null;
    const saved = snapshot(response, await response.json(), decode);
    return Number.isFinite(saved.timestamp) ? saved : null;
  } catch {
    return null;
  }
}

export async function loadSnapshot(path, decode, { fetcher = fetch, storage } = {}) {
  const url = new URL(path, import.meta.url).href;
  const cache = openCache(storage);
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8_000);
  let response;
  let fresh;
  try {
    response = await fetcher(url, { cache: "no-cache", signal: controller.signal });
    if (!response.ok) throw new Error("Данные недоступны");
    fresh = snapshot(response, await response.clone().json(), decode);
  } catch (error) {
    const previous = await savedSnapshot(cache, url, decode);
    if (!previous) throw error;
    return { ...previous, cached: true };
  } finally {
    clearTimeout(timeout);
  }
  const previous = await savedSnapshot(cache, url, decode);
  if (previous && !(fresh.timestamp >= previous.timestamp)) {
    return { ...previous, cached: true };
  }
  if (Number.isFinite(fresh.timestamp) && fresh.timestamp !== previous?.timestamp) {
    try {
      await (await cache)?.put(url, response);
    } catch { /* Storage is optional in a Telegram webview. */ }
  }
  return { ...fresh, cached: false };
}

export function snapshotNotice(stamp) {
  return stamp ? `Обновление недоступно. Показан снимок от ${stamp} по Белграду.`
    : "Обновление недоступно. Показаны последние загруженные данные.";
}
