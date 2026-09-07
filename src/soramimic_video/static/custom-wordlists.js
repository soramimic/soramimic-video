// Named lists stay in this browser. The legacy source remains intact as a backup;
// an IndexedDB record marks migration complete, including when every list is deleted.
globalThis.VideoCustomWordlists = {
  createRepository(options = {}) {
    const key = "soramimic-video-custom-wordlists";
    const storeName = "repository";
    const stateKey = "state";
    let opening;
    const storageError = (error) => new Error(
      error?.name === "QuotaExceededError"
        ? "自作リストを保存できません。ブラウザの空き容量を確認してください。既存のデータは変更していません。"
        : "自作リストの保存領域にアクセスできません。ブラウザの保存設定を確認して、再試行してください。",
      { cause: error },
    );
    function legacyState() {
      let raw;
      try {
        const storage = Object.hasOwn(options, "legacyStorage") ? options.legacyStorage : globalThis.localStorage;
        raw = storage?.getItem(key) ?? null;
      } catch (error) { throw storageError(error); }
      if (raw === null) return { version: 1, lists: [] };
      let state;
      try { state = JSON.parse(raw); } catch { /* validated below */ }
      const ids = new Set();
      if (!state || state.version !== 1 || !Array.isArray(state.lists)
          || state.lists.some((list) => {
            if (!list || typeof list.id !== "string" || !list.id || ids.has(list.id)
                || typeof list.name !== "string" || typeof list.text !== "string") return true;
            ids.add(list.id);
            return false;
          })) {
        throw new Error("保存された自作リストを読み取れません。データは上書きしていません。");
      }
      return state;
    }
    function open() {
      if (opening) return opening;
      opening = new Promise((resolve, reject) => {
        let request;
        let abandoned = false;
        try {
          const factory = Object.hasOwn(options, "indexedDB") ? options.indexedDB : globalThis.indexedDB;
          if (!factory) throw new Error("IndexedDB unavailable");
          request = factory.open(key, 1);
        } catch (error) { reject(storageError(error)); return; }
        request.onupgradeneeded = () => request.result.createObjectStore(storeName);
        request.onerror = () => reject(storageError(request.error));
        request.onblocked = () => {
          abandoned = true;
          reject(new Error("自作リストの保存領域を開けません。ほかのタブを閉じて再試行してください。"));
        };
        request.onsuccess = () => {
          const database = request.result;
          if (abandoned) { database.close(); return; }
          database.onversionchange = () => { database.close(); opening = undefined; };
          database.onclose = () => { opening = undefined; };
          resolve(database);
        };
      }).catch((error) => { opening = undefined; throw error; });
      return opening;
    }
    async function transact(change) {
      const database = await open();
      return new Promise((resolve, reject) => {
        let transaction;
        try { transaction = database.transaction(storeName, "readwrite"); }
        catch (error) { opening = undefined; reject(storageError(error)); return; }
        let result;
        let failure;
        transaction.oncomplete = () => resolve(result);
        transaction.onabort = () => reject(failure || storageError(transaction.error));
        transaction.onerror = () => { /* onabort reports failure after rollback */ };
        const store = transaction.objectStore(storeName);
        const request = store.get(stateKey);
        request.onsuccess = () => {
          try {
            const state = request.result ?? legacyState();
            result = change ? change(state) : state.lists;
            // The first successful transaction stores both migration and mutation.
            // Every tab reads and writes within this transaction to avoid lost updates.
            if (change || request.result === undefined) store.put(state, stateKey);
          } catch (error) {
            failure = error?.name === "Error" ? error : storageError(error);
            transaction.abort();
          }
        };
      });
    }
    return {
      lists: () => transact(),
      async save({ id, name, text }) {
        name = String(name || "").trim();
        text = String(text || "").trim();
        if (!name) throw new Error("リスト名を入力してください。");
        if (name.length > 100) throw new Error("リスト名は100文字以内にしてください。");
        if (!text) throw new Error("単語を入力してください。");
        return transact((state) => {
          const index = id ? state.lists.findIndex((list) => list.id === id) : -1;
          if (id && index < 0) throw new Error("このリストは削除されています。新しいリストとして登録してください。");
          const now = new Date().toISOString();
          const list = {
            id: id || globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`,
            name, text, createdAt: index < 0 ? now : state.lists[index].createdAt, updatedAt: now,
          };
          if (index < 0) state.lists.push(list); else state.lists[index] = list;
          return list;
        });
      },
      remove: (id) => transact((state) => {
        state.lists = state.lists.filter((list) => list.id !== id);
      }),
    };
  },
};
