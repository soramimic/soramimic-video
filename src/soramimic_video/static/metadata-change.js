// Remember first-upload notices, song-replacement choices and word-list choices separately.
(() => {
  let pending = Promise.resolve();
  function notifyChange(kind, isCurrent = () => true) {
    const ask = () => {
      if (!isCurrent()) return;
      const key = `soramimic-metadata-notice-hidden-${kind}`;
      try {
        const saved = localStorage.getItem(key);
        if (kind === "song" && saved === "1") return;
        if (kind !== "song" && ["keep", "update"].includes(saved)) return saved;
      } catch { /* Continue with a visible choice when storage is unavailable. */ }
      const dialog = document.getElementById("metadata-change-dialog");
      const checkbox = document.getElementById("metadata-change-hide-warning");
      const replacingSong = kind === "song-replacement";
      document.getElementById("metadata-change-title").textContent = kind === "song"
        ? "曲情報は保持されています" : replacingSong ? "曲情報を更新しますか？" : "単語リスト名を更新しますか？";
      document.getElementById("metadata-change-hint").textContent = kind === "song"
        ? "音源を変更しました。入力済みの曲名・クレジットは保持しています。新しい音源に合っているか確認し、必要に応じて編集してください。"
        : replacingSong
          ? "音源を差し替えました。更新すると曲名を新しいファイル名に変更し、入力済みのクレジットを消去します。"
          : "単語リストを変更しました。入力済みの表示用の名前を保持するか、選択したリストの名前に更新するか選んでください。";
      document.getElementById("metadata-change-acknowledge").hidden = kind !== "song";
      document.getElementById("metadata-change-keep").hidden = kind === "song";
      document.getElementById("metadata-change-update").hidden = kind === "song";
      document.getElementById("metadata-change-keep").textContent = replacingSong
        ? "入力済みの曲情報を保持" : "入力済みの名前を保持";
      document.getElementById("metadata-change-update").textContent = replacingSong
        ? "曲名を更新・クレジットを消去" : "選択したリスト名に更新";
      document.getElementById("metadata-change-preference-label").textContent = kind === "song"
        ? "次回から表示しない" : "次回から表示しない（この選択を記憶）";
      checkbox.checked = false;
      dialog.returnValue = "";
      return new Promise((resolve) => {
        dialog.addEventListener("close", () => {
          const choice = dialog.returnValue;
          const accepted = kind === "song" ? choice === "acknowledge"
            : ["keep", "update"].includes(choice);
          if (isCurrent() && checkbox.checked && accepted) {
            try { localStorage.setItem(key, kind === "song" ? "1" : choice); }
            catch { /* Show again next time. */ }
          }
          resolve(kind !== "song" && choice === "update" ? "update" : "keep");
        }, { once: true });
        dialog.showModal();
        document.getElementById(kind === "song" ? "metadata-change-acknowledge"
          : "metadata-change-keep").focus();
      });
    };
    const result = pending.then(ask);
    pending = result.catch(() => {});
    return result;
  }
  window.VideoMetadata = { notifyChange };
})();
