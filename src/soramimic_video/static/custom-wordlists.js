// Named lists stay on this browser; writes fail without replacing existing data.
globalThis.VideoCustomWordlists = {
  createRepository(storage) {
    const key = "soramimic-video-custom-wordlists";
    function read() {
      const raw = storage.getItem(key);
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
    function write(state) {
      try { storage.setItem(key, JSON.stringify(state)); }
      catch { throw new Error("自作リストを保存できません。ブラウザの空き容量と保存設定を確認してください。"); }
    }
    return {
      lists: () => read().lists,
      save({ id, name, text }) {
        name = String(name || "").trim();
        text = String(text || "").trim();
        if (!name) throw new Error("リスト名を入力してください。");
        if (name.length > 100) throw new Error("リスト名は100文字以内にしてください。");
        if (!text) throw new Error("単語を入力してください。");
        const state = read();
        const index = id ? state.lists.findIndex((list) => list.id === id) : -1;
        if (id && index < 0) throw new Error("このリストは削除されています。新しいリストとして登録してください。");
        const now = new Date().toISOString();
        const list = {
          id: id || globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`,
          name, text, createdAt: index < 0 ? now : state.lists[index].createdAt, updatedAt: now,
        };
        if (index < 0) state.lists.push(list); else state.lists[index] = list;
        write(state);
        return list;
      },
      remove(id) {
        const state = read();
        state.lists = state.lists.filter((list) => list.id !== id);
        write(state);
      },
    };
  },
};
