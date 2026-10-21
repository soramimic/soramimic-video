// Resume transfers in the same page; audio bytes never enter browser storage.
(() => {
  const sessions = new WeakMap();
  let previousFile = null;
  const discard = file => {
    const session = sessions.get(file);
    if (session?.active) session.discarded = true;
    else return session?.remove?.();
  };
  const selectionChanged = file => {
    if (previousFile && previousFile !== file) return discard(previousFile);
  };
  const abortError = () => new DOMException("音源送信を中断しました", "AbortError");
  const wait = (ms, signal) => new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(abortError());
    const abort = () => { clearTimeout(timer); reject(abortError()); };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", abort); resolve();
    }, ms);
    signal?.addEventListener("abort", abort, {once: true});
  });

  async function send(form, {headers, signal, onStatus}) {
    const file = form.get("audio");
    await selectionChanged(file);
    previousFile = file;
    let session = sessions.get(file);
    if (!session) {
      session = {id: form.get("submission_id")};
      sessions.set(file, session);
    }
    form.set("submission_id", session.id);
    const url = `/api/audio-uploads/${session.id}`;
    const request = async (target, options = {}, retries = 4, timeout = 30000) => {
      for (let attempt = 0; ; attempt++) {
        if (signal?.aborted) throw abortError();
        const ctrl = new AbortController();
        const abort = () => ctrl.abort();
        signal?.addEventListener("abort", abort, {once: true});
        const timer = setTimeout(abort, timeout);
        try {
          const response = await fetch(target, {...options, headers: {...headers(), ...options.headers},
            cache: "no-store", signal: ctrl.signal});
          const body = await response.json();
          if (response.status >= 500) {
            throw new Error(body.detail || "サーバーの応答を確認できませんでした");
          }
          return {ok: response.ok, status: response.status, body, json: async () => body};
        } catch (error) {
          if (signal?.aborted) throw abortError();
          if (attempt >= retries) throw error;
          onStatus("通信の回復を待っています…");
        } finally {
          clearTimeout(timer);
          signal?.removeEventListener("abort", abort);
        }
        await wait(Math.min(1000 * 2 ** attempt, 15000), signal);
      }
    };
    const remove = (cancelJob = false) => {
      sessions.delete(file);
      // Cancellation must return immediately, including when the connection is down.
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 5000);
      const target = cancelJob ? `${url}?cancel_job=true` : url;
      return fetch(target, {method: "DELETE", headers: headers(), signal: ctrl.signal, keepalive: true})
        .catch(() => {}).finally(() => clearTimeout(timer));
    };
    session.remove = remove;
    const check = response => {
      if (!response.ok) throw new Error(typeof response.body.detail === "string"
        ? response.body.detail : "音源の送信を確認できませんでした");
      const info = response.body;
      if (!Number.isInteger(info.offset) || info.offset < 0 || info.offset > file.size
          || info.size !== file.size || !Number.isInteger(info.chunk_bytes) || info.chunk_bytes <= 0) {
        throw new Error("音源の送信位置を確認できませんでした");
      }
      return info;
    };
    try {
      session.active = true;
      onStatus("音源の送信を準備しています…");
      const init = new URLSearchParams({filename: file.name, size: String(file.size),
        submission_id: session.id, turnstile_token: form.get("turnstile_token") || ""});
      const started = await request("/api/audio-uploads", {method: "POST", body: init});
      if (!started.ok) return started; // The caller can renew a rejected verification token.
      if (typeof started.body.job_id === "string") {
        sessions.delete(file);
        const body = {id: started.body.job_id};
        return {ok: true, status: 200, json: async () => body};
      }
      let info = check(started), offset = info.offset;
      let chunkBytes = Math.min(info.chunk_bytes, 64 * 1024), failures = 0, goodChunks = 0;
      while (offset < file.size) {
        if (signal?.aborted) throw abortError();
        onStatus(`音源を送信中… ${Math.min(99, Math.floor(offset * 100 / file.size))}%`);
        try {
          const sentAt = Date.now();
          const chunk = file.slice(offset, offset + chunkBytes);
          const response = await request(`${url}?offset=${offset}`,
            {method: "PUT", body: chunk, headers: {"Content-Type": "application/octet-stream"}}, 0);
          if (response.status === 409) {
            info = check(await request(url));
          } else {
            if (!response.ok) { remove(); return response; }
            info = check(response);
          }
          if (info.offset <= offset) throw new Error("送信が進みませんでした");
          offset = info.offset;
          failures = 0;
          if (Date.now() - sentAt < 1000) {
            if (++goodChunks >= 3) {
              chunkBytes = Math.min(info.chunk_bytes, chunkBytes * 2);
              goodChunks = 0;
            }
          } else goodChunks = 0;
        } catch (error) {
          if (signal?.aborted) throw abortError();
          if (++failures > 5) throw error;
          chunkBytes = Math.max(8192, Math.floor(chunkBytes / 2));
          goodChunks = 0;
          onStatus("通信の回復を待っています… 届いた部分から再開します");
          await wait(Math.min(1000 * 2 ** (failures - 1), 15000), signal);
          info = check(await request(url));
          offset = info.offset;
        }
      }
      onStatus("音源を受信しました。生成の受付を確認しています…");
      const metadata = new FormData();
      for (const [key, value] of form.entries()) if (key !== "audio") metadata.append(key, value);
      metadata.set("audio_upload_id", session.id);
      session.finalizing = true;
      const response = await request("/api/jobs", {method: "POST", body: metadata}, 4, 120000);
      if (response.ok) {
        if (typeof response.body.id !== "string") throw new Error("生成の受付を確認できませんでした");
        sessions.delete(file);
      } else if (response.status !== 429) remove();
      return response;
    } catch (error) {
      if (signal?.aborted) { remove(true); throw abortError(); }
      // Keep this File's transfer ID so another tap can resume acknowledged bytes.
      if (session.finalizing) {
        throw Object.assign(new Error("生成の受付を確認できませんでした。通信が戻ったら、"
          + "同じ音源のままもう一度お試しください。", {cause: error}),
        {submissionUnconfirmed: true});
      }
      throw new Error("音源の送信を完了できませんでした。通信が戻ったらもう一度お試しください。",
        {cause: error});
    } finally {
      session.active = false;
      if (session.discarded && sessions.get(file) === session) remove();
    }
  }
  globalThis.SoramimicAudioUpload = {send, discard, selectionChanged};
})();
