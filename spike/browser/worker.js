// 空耳変換をWorker内で完結させる(UIスレッドをブロックしないことの確認用)。
/* eslint-env worker */
importScripts('/kuromoji.js');

self.onmessage = async (e) => {
  try {
    const { run } = await import('/engine.js');
    const out = await run(e.data);
    self.postMessage({ ok: true, ...out });
  } catch (err) {
    self.postMessage({ ok: false, error: String((err && err.stack) || err) });
  }
};
