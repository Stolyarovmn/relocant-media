const prefix = "relocant_agenda_favorite_";

function cloudRequest(call) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Хранилище не ответило")), 5000);
    const finish = (error, value) => {
      clearTimeout(timer);
      if (error) reject(error);
      else resolve(value);
    };
    try {
      call(finish);
    } catch (error) {
      finish(error);
    }
  });
}

export function agendaFavorites(telegram, getStorage = () => localStorage) {
  const cloud = telegram?.initData && telegram.isVersionAtLeast?.("6.9") && telegram.CloudStorage;
  if (cloud?.getKeys && cloud.setItem && cloud.removeItem) {
    return {
      async read() {
        const keys = await cloudRequest((done) => cloud.getKeys(done));
        if (!Array.isArray(keys)) throw new Error("Не удалось прочитать избранное");
        return new Set(keys.filter((key) => typeof key === "string" && key.startsWith(prefix))
          .map((key) => key.slice(prefix.length)));
      },
      async write(id, saved) {
        const key = prefix + id;
        const stored = await cloudRequest((done) => saved ? cloud.setItem(key, "1", done) : cloud.removeItem(key, done));
        if (stored !== true) throw new Error("Избранное не изменилось");
      },
    };
  }
  return {
    async read() {
      const storage = getStorage();
      const favorites = new Set();
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (key?.startsWith(prefix)) favorites.add(key.slice(prefix.length));
      }
      return favorites;
    },
    async write(id, saved) {
      const storage = getStorage();
      if (saved) storage.setItem(prefix + id, "1");
      else storage.removeItem(prefix + id);
    },
  };
}
