/* Keep cancellation attached to the server reservation, even before a job ID arrives. */
(() => {
  const forms = new WeakMap(), audio = new WeakMap();
  async function request(url, options, signal) {
    const ctrl = new AbortController();
    const abort = () => ctrl.abort();
    signal?.addEventListener('abort', abort, {once: true});
    if (signal?.aborted) ctrl.abort();
    const timer = setTimeout(abort, 10000);
    try {
      const res = await fetch(url, {...options, signal: ctrl.signal, cache: 'no-store'});
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || '受付情報を確認できませんでした');
      return body;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }
  const finish = form => {
    const state = forms.get(form), file = form.get('audio');
    if (file && audio.get(file) === state) audio.delete(file);
  };
  globalThis.SoramimicSubmissions = {
    async prepare(form, {headers, signal}) {
      const file = form.get('audio');
      let state = file && audio.get(file);
      if (!state) state = {id: null, sent: false};
      forms.set(form, state);
      if (file) audio.set(file, state);
      if (!state.id) {
        const body = await request('/api/submissions', {method: 'POST', headers: headers()}, signal);
        signal?.throwIfAborted();
        if (!/^[a-f0-9]{32}$/.test(body.id || '')) throw new Error('受付情報を確認できませんでした');
        state.id = body.id;
      }
      signal?.throwIfAborted();
      form.set('submission_id', state.id);
      form.set('registered_submission', 'true');
    },
    sent(form) { const state = forms.get(form); if (state) state.sent = true; },
    wasSent(form) { return !!forms.get(form)?.sent; },
    async lookup(form, headers, signal) {
      const state = forms.get(form);
      if (!state?.id) throw new Error('受付情報を確認できませんでした');
      const body = await request(`/api/submissions/${state.id}`, {headers: headers()}, signal);
      if ((body.id === null && body.status === 'pending')
          || (typeof body.id === 'string' && /^[a-f0-9]{8}$/.test(body.id)
              && ['queued', 'running', 'awaiting_review', 'done', 'error', 'canceled'].includes(body.status))) {
        return body;
      }
      throw new Error('受付情報を確認できませんでした');
    },
    async cancel(form, headers) {
      const state = forms.get(form);
      const body = state?.id ? await request(`/api/submissions/${state.id}/cancel`,
        {method: 'POST', headers: headers()}) : {id: null, status: 'canceled'};
      if (!['queued', 'running', 'awaiting_review', 'done', 'error', 'canceled'].includes(body.status)
          || (body.id !== null && (typeof body.id !== 'string' || !/^[a-f0-9]{8}$/.test(body.id)))
          || (body.id === null && body.status !== 'canceled')) {
        throw new Error('中断の結果を確認できませんでした');
      }
      finish(form);
      return body;
    },
    finish,
  };
})();
