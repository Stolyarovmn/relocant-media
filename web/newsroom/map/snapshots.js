const cacheName = "relocant-map-public-feeds-v1";

async function openCache(storage) {
  try {
    return await (storage ?? globalThis.caches)?.open(cacheName);
  } catch {
    return undefined;
  }
}

function snapshot(data, decode) {
  const generatedAt = typeof data?.generated_at === "string" ? data.generated_at : "";
  return {
    entries: decode(data),
    generatedAt,
    timestamp: Date.parse(generatedAt),
  };
}

async function savedSnapshot(cache, url, decode) {
  try {
    const response = await (await cache)?.match(url);
    if (!response) return null;
    const saved = snapshot(await response.json(), decode);
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
    fresh = snapshot(await response.clone().json(), decode);
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
