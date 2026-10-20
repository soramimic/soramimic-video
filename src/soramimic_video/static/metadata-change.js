// Remember song-title choices and word-list choices separately.
(() => {
  let pending = Promise.resolve();
  function notifyChange(kind, isCurrent = () => true) {
    const ask = () => {
      if (!isCurrent()) return;
      const preference = kind === "song-replacement" ? "song-title-v2" : kind;
      const key = `soramimic-metadata-notice-hidden-${preference}`;
      try {
        const saved = localStorage.getItem(key);
        if (["keep", "update"].includes(saved)) return saved;
      } catch { /* Continue with a visible choice when storage is unavailable. */ }
      const dialog = document.getElementById("metadata-change-dialog");
      const checkbox = document.getElementById("metadata-change-hide-warning");
      const replacingSong = kind === "song-replacement";
      document.getElementById("metadata-change-title").textContent = replacingSong
        ? "曲名をどうしますか？" : "単語リスト名を更新しますか？";
      document.getElementById("metadata-change-hint").textContent = replacingSong
        ? "入力済みの曲名を保持するか、ファイル名を使うか選んでください。クレジット表記は保持します。"
        : "単語リストを変更しました。入力済みの表示用の名前を保持するか、選択したリストの名前に更新するか選んでください。";
      document.getElementById("metadata-change-keep").textContent = replacingSong
        ? "入力済みの曲名を保持" : "入力済みの名前を保持";
      document.getElementById("metadata-change-update").textContent = replacingSong
        ? "ファイル名を使う" : "選択したリスト名に更新";
      document.getElementById("metadata-change-preference-label").textContent =
        "次回から表示しない（この選択を記憶）";
      checkbox.checked = false;
      dialog.returnValue = "";
      return new Promise((resolve) => {
        dialog.addEventListener("close", () => {
          const choice = dialog.returnValue;
          const accepted = ["keep", "update"].includes(choice);
          if (isCurrent() && checkbox.checked && accepted) {
            try { localStorage.setItem(key, choice); }
            catch { /* Show again next time. */ }
          }
          resolve(choice === "update" ? "update" : "keep");
        }, { once: true });
        dialog.showModal();
        document.getElementById("metadata-change-keep").focus();
      });
    };
    const result = pending.then(ask);
    pending = result.catch(() => {});
    return result;
  }
  window.VideoMetadata = { notifyChange };
})();
