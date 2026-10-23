import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {File} from 'node:buffer';
import vm from 'node:vm';
import test from 'node:test';

const source = readFileSync('src/soramimic_video/static/audio-upload.js', 'utf8');
const bytes = Buffer.from(Array.from({length: 180000}, (_, i) => i % 251));
const response = (body, status = 200) => ({ok: status < 400, status, json: async () => body});

function setup(hook = () => {}, data = bytes) {
  const stages = new Map(), jobs = new Map(), calls = [], statuses = [], timers = new Map();
  class Surface extends EventTarget {
    listeners = new Set();
    addEventListener(type, listener, options) {
      this.listeners.add(listener);
      super.addEventListener(type, listener, options);
    }
    removeEventListener(type, listener) {
      this.listeners.delete(listener);
      super.removeEventListener(type, listener);
    }
  }
  const page = new Surface(), window = new Surface();
  page.hidden = false;
  const visibility = hidden => {
    page.hidden = hidden;
    page.dispatchEvent(new Event('visibilitychange'));
  };
  let creates = 0, timer = 0;
  const fetch = async (url, options = {}) => {
    assert.equal(options.headers['X-API-Key'], 'key');
    const method = options.method || 'GET';
    calls.push({url, method});
    const sid = url.split('/')[3]?.split('?')[0];
    const before = await hook({url, method, options, stages, jobs, calls, timers, visibility});
    if (before) return before;
    if (url === '/api/audio-uploads' && method === 'POST') {
      const id = options.body.get('submission_id');
      if (jobs.has(id)) return response({id, job_id: jobs.get(id)});
      if (!stages.has(id)) stages.set(id, {data: Buffer.alloc(0), size: Number(options.body.get('size'))});
      const stage = stages.get(id);
      return response({id, size: stage.size, offset: stage.data.length, chunk_bytes: 524288});
    }
    if (method === 'DELETE') {stages.delete(sid); return response({removed: true});}
    if (url === '/api/jobs') {
      const id = options.body.get('submission_id');
      assert.equal(options.body.has('audio'), false, 'final request contains no audio bytes');
      assert.equal(options.body.get('audio_upload_id'), id);
      if (!jobs.has(id)) {
        assert.deepEqual(stages.get(id).data, data);
        creates++;
        jobs.set(id, 'job'); stages.delete(id);
      }
      return response({id: jobs.get(id)});
    }
    const stage = stages.get(sid);
    if (!stage) return response({detail: 'expired'}, 404);
    if (method === 'PUT') {
      const offset = Number(new URL(url, 'https://test.invalid').searchParams.get('offset'));
      const chunk = Buffer.from(await options.body.arrayBuffer());
      if (offset !== stage.data.length) return response({detail: 'offset conflict'}, 409);
      stage.data = Buffer.concat([stage.data, chunk]);
    }
    return response({id: sid, size: stage.size, offset: stage.data.length, chunk_bytes: 524288});
  };
  const context = vm.createContext({fetch, FormData, URLSearchParams, AbortController, DOMException,
    document: page, addEventListener: window.addEventListener.bind(window),
    removeEventListener: window.removeEventListener.bind(window),
    setTimeout(callback, ms) {
      const id = ++timer;
      if (ms < 30000) queueMicrotask(callback); else timers.set(id, callback);
      return id;
    }, clearTimeout(id) {timers.delete(id);}, console});
  vm.runInContext(source, context);
  const file = new File([data], 'song.mp3', {lastModified: 1});
  const form = (id = 'a'.repeat(32)) => {
    const data = new FormData(); data.set('audio', file); data.set('submission_id', id);
    data.set('turnstile_token', 'once'); data.set('wordlist', 'stations');
    return data;
  };
  const send = (data = form(), signal = undefined) => context.SoramimicAudioUpload.send(data, {
    headers: () => ({'X-API-Key': 'key'}), onStatus: status => statuses.push(status), signal,
  });
  return {stages, jobs, calls, statuses, file, form, send, creates: () => creates,
    selectionChanged: context.SoramimicAudioUpload.selectionChanged, visibility, page, window};
}

test('a dropped acknowledgement resumes from server bytes, without altering the audio', async () => {
  let lost = false;
  const h = setup(async ({method, options, stages}) => {
    if (method === 'PUT' && !lost) {
      lost = true;
      const stage = [...stages.values()][0];
      stage.data = Buffer.from(await options.body.arrayBuffer());
      throw new TypeError('Load failed after server committed bytes');
    }
  });
  const result = await h.send();
  assert.equal((await result.json()).id, 'job');
  const puts = h.calls.filter(call => call.method === 'PUT');
  assert.ok(puts[1].url.endsWith('offset=65536'), 'acknowledged bytes are not resent');
  assert.equal(h.creates(), 1);
  assert.ok(h.statuses.some(text => text.includes('届いた部分')));
});

test('a later tap retains the transfer ID and offset after prolonged interruption', async () => {
  let offline = true;
  const h = setup(({method, stages}) => {
    if (method === 'PUT' && offline && [...stages.values()][0].data.length) {
      throw new TypeError('network unavailable');
    }
  });
  await assert.rejects(h.send(), /もう一度/);
  assert.equal(h.stages.get('a'.repeat(32)).data.length, 65536);
  const n = h.calls.length;
  offline = false;
  const retryForm = h.form('b'.repeat(32));
  assert.equal(retryForm.get('audio'), h.file);
  assert.equal((await (await h.send(retryForm)).json()).id, 'job');
  assert.equal(retryForm.get('submission_id'), 'a'.repeat(32));
  assert.ok(h.calls.slice(n).find(c => c.method === 'PUT').url.endsWith('offset=65536'));
  assert.equal(h.creates(), 1);
});

test('lost job acknowledgement is retried without transferring audio again', async () => {
  let lost = false;
  const h = setup(({url, options, stages, jobs}) => {
    if (url === '/api/jobs' && !lost) {
      lost = true;
      const id = options.body.get('submission_id');
      assert.deepEqual(stages.get(id).data, bytes);
      stages.delete(id); jobs.set(id, 'job');
      throw new TypeError('response lost');
    }
  });
  assert.equal((await (await h.send()).json()).id, 'job');
  assert.equal(h.jobs.size, 1);
  assert.equal(h.calls.filter(call => call.url === '/api/jobs').length, 2);
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, 3);
});

test('an expired single-use verification token is renewed before any audio is sent', async () => {
  let rejected = false;
  const h = setup(({url}) => {
    if (url === '/api/audio-uploads' && !rejected) {
      rejected = true; return response({detail: 'verify again'}, 403);
    }
  });
  const form = h.form();
  assert.equal((await h.send(form)).status, 403);
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 0);
  form.set('turnstile_token', 'fresh');
  assert.equal((await (await h.send(form)).json()).id, 'job');
});

test('cancelling an active chunk deletes the partial transfer and creates no job', async () => {
  const controller = new AbortController();
  let stopped = false;
  const h = setup(({method, options, stages}) => {
    if (method === 'PUT' && [...stages.values()][0].data.length && !stopped) {
      stopped = true;
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new DOMException('canceled', 'AbortError')));
        queueMicrotask(() => controller.abort());
      });
    }
  });
  await assert.rejects(h.send(h.form(), controller.signal), error => error.name === 'AbortError');
  assert.equal(h.stages.size, 0);
  assert.equal(h.creates(), 0);
});

test('a hung chunk times out and is retried in smaller pieces', async () => {
  let timedOut = false;
  const sizes = [];
  const h = setup(({method, options, timers}) => {
    if (method === 'PUT') {
      sizes.push(options.body.size);
      if (!timedOut) {
        timedOut = true;
        return new Promise((_resolve, reject) => {
          options.signal.addEventListener('abort', () => reject(new DOMException('timeout', 'AbortError')));
          const timeout = [...timers.values()].at(-1);
          queueMicrotask(timeout);
        });
      }
    }
  });
  assert.equal((await (await h.send()).json()).id, 'job');
  assert.deepEqual(sizes.slice(0, 2), [65536, 32768]);
  assert.equal(h.creates(), 1);
});

test('quota errors retain the original response and do not trigger upload retries', async () => {
  const h = setup(() => response({detail: 'quota exceeded'}, 429));
  const result = await h.send();
  assert.equal(result.status, 429);
  assert.equal((await result.json()).detail, 'quota exceeded');
  assert.equal(h.calls.length, 1);
});

test('healthy transfers grow bounded chunks to avoid unnecessary round trips', async () => {
  const sizes = [];
  const h = setup(({method, options}) => {
    if (method === 'PUT') sizes.push(options.body.size);
  }, Buffer.alloc(2 * 1024 * 1024));
  assert.equal((await (await h.send()).json()).id, 'job');
  assert.equal(sizes[0], 65536);
  assert.ok(sizes.some(size => size > 65536));
  assert.ok(sizes.every(size => size <= 524288));
  assert.equal(h.creates(), 1);
});

test('a final quota rejection retains uploaded audio for a later tap', async () => {
  let limited = true;
  const h = setup(({url}) => {
    if (url === '/api/jobs' && limited) return response({detail: 'queue full'}, 429);
  });
  assert.equal((await h.send()).status, 429);
  assert.equal(h.stages.size, 1);
  const puts = h.calls.filter(call => call.method === 'PUT').length;
  limited = false;
  assert.equal((await (await h.send(h.form('b'.repeat(32)))).json()).id, 'job');
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, puts);
  assert.equal(h.creates(), 1);
});

test('uncertain job acceptance can be recovered by another tap without a duplicate generation', async () => {
  let unavailable = true;
  const h = setup(({url, options, stages, jobs}) => {
    if (url === '/api/jobs' && unavailable) {
      const id = options.body.get('submission_id');
      if (!jobs.has(id)) {
        assert.deepEqual(stages.get(id).data, bytes);
        stages.delete(id); jobs.set(id, 'job');
      }
      throw new TypeError('all acknowledgements lost');
    }
  });
  await assert.rejects(h.send(), error => error.submissionUnconfirmed === true);
  assert.equal(h.jobs.size, 1);
  const puts = h.calls.filter(call => call.method === 'PUT').length;
  unavailable = false;
  assert.equal((await (await h.send(h.form('b'.repeat(32)))).json()).id, 'job');
  assert.equal(h.calls.filter(call => call.method === 'PUT').length, puts);
  assert.equal(h.jobs.size, 1);
});

test('selecting a different file removes an abandoned transfer instead of holding capacity', async () => {
  const h = setup(({method, stages}) => {
    if (method === 'PUT' && [...stages.values()][0].data.length) throw new TypeError('offline');
  });
  await assert.rejects(h.send(), /もう一度/);
  assert.equal(h.stages.size, 1);
  await h.selectionChanged(new File([bytes], 'another.mp3'));
  assert.equal(h.stages.size, 0);
  assert.equal(h.jobs.size, 0);
});

const nextTurn = () => new Promise(resolve => setImmediate(resolve));
const noListeners = h => {
  assert.equal(h.page.listeners.size, 0);
  assert.equal(h.window.listeners.size, 0);
};

test('returning to the page recovers a suspended chunk with a lost acknowledgement', {timeout: 2000}, async () => {
  let suspended = false;
  const h = setup(async ({method, options, stages, visibility}) => {
    if (method === 'PUT' && !suspended) {
      suspended = true;
      [...stages.values()][0].data = Buffer.from(await options.body.arrayBuffer());
      visibility(true);
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new DOMException('suspended', 'AbortError')));
      });
    }
  });
  const sent = h.send();
  await nextTurn();
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 1);
  h.visibility(false);
  assert.equal((await (await sent).json()).id, 'job');
  assert.equal(h.creates(), 1);
  assert.ok(h.calls.some(c => c.method === 'GET'), 'resume reconciles committed server bytes');
  noListeners(h);
});

test('background interruptions wait for visibility without exhausting retries', {timeout: 2000}, async () => {
  let interrupted = 0;
  const h = setup(({method, visibility}) => {
    if (method === 'PUT' && interrupted < 7) {
      interrupted++;
      visibility(true);
      throw new TypeError('background connection stopped');
    }
  });
  const sent = h.send();
  for (let i = 1; i <= 7; i++) {
    await nextTurn();
    assert.equal(interrupted, i);
    const count = h.calls.length;
    await nextTurn();
    assert.equal(h.calls.length, count, 'no retries are spent while hidden');
    h.visibility(false);
  }
  assert.equal((await (await sent).json()).id, 'job');
  assert.equal(h.creates(), 1);
  noListeners(h);
});

test('cancel while waiting in the background removes the transfer and visibility listeners', {timeout: 2000}, async () => {
  const controller = new AbortController();
  const h = setup(({method, visibility}) => {
    if (method === 'PUT') {
      visibility(true);
      throw new TypeError('suspended');
    }
  });
  const sent = h.send(h.form(), controller.signal);
  const cancelled = assert.rejects(sent, error => error.name === 'AbortError');
  await nextTurn();
  controller.abort();
  await cancelled;
  h.visibility(false);
  await nextTurn();
  assert.equal(h.stages.size, 0);
  assert.equal(h.creates(), 0);
  noListeners(h);
});

test('a healthy background connection is allowed to complete', async () => {
  const h = setup(({method, visibility}) => { if (method === 'PUT') visibility(true); });
  assert.equal((await (await h.send()).json()).id, 'job');
  assert.equal(h.creates(), 1);
  noListeners(h);
});

test('restoring a cached page recovers suspended job acceptance without a duplicate', {timeout: 2000}, async () => {
  let suspended = false;
  const h = setup(({url, options, stages, jobs}) => {
    if (url === '/api/jobs' && !suspended) {
      suspended = true;
      const id = options.body.get('submission_id');
      assert.deepEqual(stages.get(id).data, bytes);
      stages.delete(id); jobs.set(id, 'job');
      return new Promise((_resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(new DOMException('suspended', 'AbortError')));
      });
    }
  });
  const sent = h.send();
  await nextTurn();
  const event = new Event('pageshow');
  event.persisted = true;
  h.window.dispatchEvent(event);
  assert.equal((await (await sent).json()).id, 'job');
  assert.equal(h.jobs.size, 1);
  assert.equal(h.calls.filter(c => c.method === 'PUT').length, 3);
  noListeners(h);
});
