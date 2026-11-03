// モデルのファイルを読む: ブラウザの中（Cache Storage）に取っておき、2 回目からはダウンロードしない。
// GitHub Pages のキャッシュの有効期限は短い（10 分）ので、自前で取っておく。ファイルの大きさを URL に付けて、
// 中身を差し替えたとき（大きさが変わったとき）は取り直す。Cache Storage が使えない所では毎回ダウンロードする。

const CACHE = "svs-models-v1";

export type Progress = (loaded: number, total: number) => void;

/** url のファイルを読む。bytes は分かっていれば（web_index.json）進み具合の分母に使う */
export async function fetchCached(url: string, bytes: number, onProgress: Progress): Promise<Uint8Array> {
  const key = `${url}?v=${bytes}`;
  let cache: Cache | null = null;
  try {
    cache = await caches.open(CACHE);
    const hit = await cache.match(key);
    if (hit) {
      const b = new Uint8Array(await hit.arrayBuffer());
      onProgress(b.length, b.length);
      return b;
    }
  } catch {
    cache = null; // プライベートウィンドウなど
  }
  const r = await fetch(url);
  if (!r.ok || !r.body) throw new Error(`モデルを読めません: ${url}（${r.status}）`);
  const total = Number(r.headers.get("content-length")) || bytes || 0;
  const reader = r.body.getReader();
  const chunks: Uint8Array[] = [];
  let loaded = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    loaded += value.length;
    onProgress(loaded, Math.max(total, loaded));
  }
  const out = new Uint8Array(loaded);
  let o = 0;
  for (const c of chunks) {
    out.set(c, o);
    o += c.length;
  }
  if (cache) {
    try {
      await cache.put(key, new Response(out, { headers: { "Content-Type": "application/octet-stream" } }));
      // 同じファイルの古い版（大きさが違う）を消す
      for (const req of await cache.keys()) {
        if (req.url.startsWith(new URL(url, location.href).href + "?v=") && !req.url.endsWith(`?v=${bytes}`)) await cache.delete(req);
      }
    } catch { /* 取っておけなくても使える */ }
  }
  return out;
}
