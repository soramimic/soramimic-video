/* Image attribution is rendered as text; only HTTP(S) metadata becomes a link. */
(() => {
  const el = (tag, text) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    return node;
  };
  function safeUrl(value) {
    try {
      const url = new URL(String(value || ""));
      return ["https:", "http:"].includes(url.protocol) ? url.href : "";
    } catch { return ""; }
  }
  function itemCard(item) {
    const card = el("article");
    card.className = "image-source-card";
    card.append(el("h3", item.original || item.surface || item.word || "画像"));
    if (item.org && item.org !== "NA") card.append(el("p", item.org));
    card.append(el("p", "クレジット: " + (item.image_credit || item.credit || "未記載（掲載元で確認してください）")));
    const usage = item.image_usage === "noncommercial_fanwork"
      ? "非営利ファン活動限定（noncommercial_fanwork）"
      : item.image_usage || "指定なし（掲載元の条件を確認）";
    card.append(el("p", "利用区分: " + usage));
    for (const [field, label] of [["image_page", "画像掲載元"], ["image_terms_page", "公式規約"]]) {
      const line = el("p", label + ": ");
      const url = safeUrl(item[field]);
      if (url) {
        const link = el("a", item[field]);
        link.href = url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        line.append(link);
      } else line.append(el("span", "未記載（掲載元で確認してください）"));
      card.append(line);
    }
    return card;
  }
  function render(container, items, {scope = "使用素材", text = null, download = null} = {}) {
    container.replaceChildren();
    const searchLabel = el("label", "人物・事務所・クレジットを検索");
    const input = el("input");
    input.type = "search";
    searchLabel.append(input);
    const count = el("p");
    count.setAttribute("role", "status");
    const list = el("div");
    const update = () => {
      const query = input.value.trim().toLocaleLowerCase();
      const filtered = items.filter(item => Object.values(item).join(" ").toLocaleLowerCase().includes(query));
      count.textContent = `${scope}：${items.length}件（表示 ${filtered.length}件）`;
      list.replaceChildren(...filtered.map(itemCard));
      if (!filtered.length) list.append(el("p", "該当する素材はありません。"));
    };
    container.append(searchLabel, count);
    if (text !== null) {
      const areaLabel = el("label", "概要欄用クレジット（使用素材全件）");
      const area = el("textarea");
      area.readOnly = true;
      area.value = text;
      area.rows = 7;
      areaLabel.append(area);
      const actions = el("div");
      actions.className = "image-source-actions";
      const copy = el("button", "クレジットをコピー");
      copy.type = "button";
      const status = el("p");
      status.setAttribute("role", "status");
      copy.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(text);
          status.textContent = "コピーしました。";
        } catch {
          area.focus();
          area.select();
          status.textContent = "クレジットを選択しました。端末のコピー操作をご利用ください。";
        }
      });
      actions.append(copy);
      if (download) {
        const save = el("button", "出典・規約を保存 (.md)");
        save.type = "button";
        save.addEventListener("click", async () => {
          save.disabled = true;
          try { await download(); status.textContent = "保存ファイルを準備しました。"; }
          catch { status.textContent = "保存できませんでした。もう一度お試しください。"; }
          finally { save.disabled = false; }
        });
        actions.append(save);
      }
      container.append(areaLabel, actions, status);
    }
    container.append(list);
    input.addEventListener("input", update);
    update();
  }
  window.ImageCredits = {safeUrl, itemCard, render};
})();
