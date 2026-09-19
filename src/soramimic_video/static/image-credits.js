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
  window.ImageCredits = {safeUrl, itemCard};
})();
