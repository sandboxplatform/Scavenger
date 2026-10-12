// Outbox of answers and photos made while offline, kept in IndexedDB so they survive
// closing the page. Falls back to memory if IndexedDB is unavailable (e.g. some private modes).
const outbox = (() => {
  function memoryOutbox() {
    let entries = [];
    let nextId = 1;
    return {
      add: async (entry) => { entries.push({ ...entry, id: nextId++ }); },
      put: async (entry) => { entries = entries.map((e) => (e.id === entry.id ? entry : e)); },
      remove: async (id) => { entries = entries.filter((e) => e.id !== id); },
      all: async () => entries.slice(),
      clear: async () => { entries = []; },
    };
  }

  function idbOutbox(db) {
    const run = (mode, fn) => new Promise((resolve, reject) => {
      const tx = db.transaction("outbox", mode);
      const req = fn(tx.objectStore("outbox"));
      tx.oncomplete = () => resolve(req.result);
      tx.onerror = tx.onabort = () => reject(tx.error);
    });
    return {
      add: (entry) => run("readwrite", (s) => s.add(entry)),
      put: (entry) => run("readwrite", (s) => s.put(entry)),
      remove: (id) => run("readwrite", (s) => s.delete(id)),
      all: () => run("readonly", (s) => s.getAll()),
      clear: () => run("readwrite", (s) => s.clear()),
    };
  }

  const ready = new Promise((resolve) => {
    try {
      const req = indexedDB.open("scavenger", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("outbox", { keyPath: "id", autoIncrement: true });
      req.onsuccess = () => resolve(idbOutbox(req.result));
      req.onerror = () => resolve(memoryOutbox());
    } catch (_) {
      resolve(memoryOutbox());
    }
  });

  const call = (name) => async (...args) => (await ready)[name](...args);
  return { add: call("add"), put: call("put"), remove: call("remove"), all: call("all"), clear: call("clear") };
})();
