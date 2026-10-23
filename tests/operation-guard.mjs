import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { parseHTML } from 'linkedom';

function setup() {
  const { document, Element, MutationObserver, Event } = parseHTML(`<!doctype html><html><body>
    <form><input type="number" id="number" value="1"><input id="title" value="original"><select><option>one</option></select>
    <button id="generate">Generate</button><button id="cancel" type="button">Cancel</button></form>
    <button id="disabled" disabled aria-disabled="true">Unavailable</button>
    <button class="info-btn">Help</button><a href="/guidelines">Guide</a>
    <a download href="/result">Save</a><div data-operation-input id="drop"></div>
    <video controls></video><button id="continue">Open save sheet</button>
    <label for="file">Choose file</label><input type="file" id="file">
    </body></html>`);
  const context = { document, Element, MutationObserver, window: document.defaultView };
  vm.createContext(context);
  vm.runInContext(readFileSync(new URL('../src/soramimic_video/static/operation-guard.js', import.meta.url), 'utf8'), context);
  const events = [];
  for (const type of ['click', 'keydown', 'beforeinput', 'drop', 'submit', 'change']) {
    document.addEventListener(type, event => events.push([type, event.target.id]));
  }
  const send = (selector, type = 'click', props = {}) => {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.assign(event, props);
    document.querySelector(selector).dispatchEvent(event);
    return event.defaultPrevented;
  };
  return { document, ops: context.VideoOperations, send, events, context };
}

test('blocks new starts, keyboard edits, uploads, drops and form submission; preserves reading and cancellation', () => {
  const { document, ops, send, events } = setup();
  const token = ops.begin();
  for (const selector of ['#generate', 'select', 'a[download]', 'label']) assert(send(selector));
  assert(send('#title', 'keydown', { key: 'x' }));
  assert(send('#title', 'beforeinput'));
  assert(send('#number', 'keydown', { key: 'ArrowUp' }));
  assert(send('#drop', 'drop'));
  assert(send('form', 'submit'));
  assert(!send('#title', 'click'), 'selection/copy remains possible');
  assert(!send('#title', 'keydown', { key: 'c', ctrlKey: true }));
  assert(!send('#title', 'keydown', { key: 'ArrowLeft' }));
  assert(!send('#generate', 'keydown', { key: 'Tab' }));
  for (const selector of ['#cancel', '.info-btn', 'a:not([download])', 'video']) assert(!send(selector));
  assert(events.some(([type, id]) => type === 'click' && id === 'cancel'));
  assert(!events.some(([type, id]) => type === 'click' && id === 'generate'));
  token.end();
  assert(!send('#generate'));
  assert(document.querySelector('#disabled').disabled, 'existing disabled state stays disabled');
  assert.equal(document.querySelector('#disabled').getAttribute('aria-disabled'), 'true');
  assert(!document.querySelector('#generate').hasAttribute('aria-disabled'));
});

test('stale completion cannot unlock a later operation; dynamically inserted history starts stay blocked', async () => {
  const { document, ops, send } = setup();
  const video = document.querySelector('video');
  const first = ops.begin();
  first.end();
  const second = ops.begin();
  first.end();
  assert(ops.isBusy());
  assert(send('#generate'));
  const button = document.createElement('button');
  button.id = 'history-play';
  document.body.append(button);
  assert(send('#history-play'), 'capture guard works before the observer runs');
  await Promise.resolve();
  assert(button.hasAttribute('data-operation-blocked'));
  assert.equal(document.querySelector('video'), video, 'media nodes are never replaced');
  second.end();
  assert(!ops.isBusy());
  assert(!send('#history-play'));
});

test('only an explicitly started continuation is permitted, and own applied values still propagate', () => {
  const { document, ops, send, events } = setup();
  const button = document.querySelector('#continue');
  const token = ops.begin();
  assert(send('#continue'));
  ops.allowContinuation(button);
  assert(!send('#continue'));
  ops.clearContinuation(button);
  assert(send('#continue'));
  document.querySelector('#title').value = 'applied';
  assert(!send('#title', 'change'), 'programmatic change is the operation applying its result');
  assert(events.some(([type]) => type === 'change'));
  // A trusted change from an already open native picker cannot start another operation.
  document.querySelector('#title').value = 'late user edit';
  assert(send('#title', 'change', { isTrusted: true }));
  assert.equal(document.querySelector('#title').value, 'applied');
  token.end();
});

test('overlapping owned work and its cancellation do not release unrelated work', () => {
  const { document, ops, send } = setup();
  const cancel = document.querySelector('#continue');
  const read = ops.begin({ allow: [cancel] });
  const save = ops.begin();
  assert(!send('#continue'));
  read.end();
  assert(send('#continue'));
  assert(send('#generate'));
  save.end();
  assert(!send('#generate'));
});

test('an already requested media download completes while a later input operation blocks new actions', async () => {
  const { document, ops, send, context } = setup();
  let respond;
  const downloads = [];
  document.addEventListener('click', event => {
    if (event.target.download === 'song.wav') downloads.push(event.target.href);
  });
  Object.assign(context, {
    Blob, AbortController, setTimeout, clearTimeout,
    headers: () => ({}),
    URL: { createObjectURL: () => 'blob:song', revokeObjectURL() {} },
    fetch: () => new Promise(resolve => { respond = resolve; }),
  });
  const source = readFileSync(new URL('../src/soramimic_video/static/index.html', import.meta.url), 'utf8');
  vm.runInContext(source.slice(source.indexOf('let resultDownloadObjectUrl ='),
    source.indexOf('function videoFileName(')), context);
  const status = document.createElement('p');
  const link = document.querySelector('a[download]');
  const pending = context.downloadResultMedia(link, 'audio/wav', status);
  const operation = ops.begin();
  assert(send('a[download]'), 'a new save must stay blocked');
  respond({
    ok: true, blob: async () => new Blob(['audio'], { type: 'audio/wav' }),
    headers: { get: () => 'attachment; filename="song.wav"' },
  });
  await pending;
  assert.deepEqual(downloads, ['blob:song'], 'the requested file must actually be handed to the browser');
  assert(status.hidden);
  assert(!document.querySelector('a[download="song.wav"]'), 'temporary download control is removed');
  assert(send('#generate'), 'finishing the download must not unlock the input operation');
  operation.end();
  assert(!send('#generate'));
});

test('an owned action keeps its continuation and cancel usable without releasing other work', () => {
  const { document, ops, send } = setup();
  const control = document.querySelector('#continue');
  let canceled = 0, finished = 0;
  const action = ops.beginAction(control, { onCancel: () => canceled++, onFinish: () => finished++ });
  assert.equal(ops.beginAction(control), action, 'second tap continues the same action');
  assert(send('#generate'));
  assert(!send('#continue'));
  const other = ops.begin();
  document.querySelector('.operation-cancel').click();
  assert(action.canceled);
  assert.equal(canceled, 1);
  assert.equal(finished, 1);
  action.finish();
  assert.equal(finished, 1);
  assert(ops.isBusy(), 'cancelling this action must not unlock unrelated work');
  other.end();
  const next = ops.beginAction(control);
  action.finish();
  assert(ops.hasAction(control), 'stale completion must not end a new request');
  next.finish();
  assert(!ops.isBusy());
  assert(!document.querySelector('.operation-cancel'));
});

test('dialog confirmation submits through its allowed button without enabling unrelated form submissions', () => {
  const { document, ops, send } = setup();
  const form = document.createElement('form');
  form.id = 'confirm'; form.setAttribute('method', 'dialog');
  const button = document.createElement('button'); button.id = 'metadata-change-update';
  form.append(button); document.body.append(form);
  Object.defineProperty(button, 'form', { value: form });
  const token = ops.begin();
  assert(!send('#metadata-change-update'));
  assert(!send('#confirm', 'submit', { submitter: button }));
  assert(send('#confirm', 'submit'), 'a submit without an allowed completion remains blocked');
  form.setAttribute('method', 'post');
  assert(send('#confirm', 'submit', { submitter: button }), 'the exception only completes native dialogs');
  token.end();
});

const pageSource = readFileSync(new URL('../src/soramimic_video/static/index.html', import.meta.url), 'utf8');
function pageFunction(name) {
  return pageSource.match(new RegExp(`^(?:async )?function ${name}\\([^\\n]*\\) \\{.*?^\\}`, 'ms'))[0];
}
function sharingSetup() {
  const h = setup(), { document, context } = h;
  const button = document.querySelector('#continue'); button.id = 'share-save';
  const hint = document.createElement('p'); hint.id = 'share-hint'; document.body.append(hint);
  const video = document.querySelector('video'); video.load = () => {}; video.pause = () => {};
  let respond, reject, shared = 0;
  Object.assign(context, {
    Blob, File, AbortController, setTimeout, clearTimeout,
    location: { origin: 'https://example.test' },
    navigator: { share: () => { shared++; return new Promise(() => {}); }, userActivation: { isActive: false } },
    URL: { createObjectURL: () => 'blob:prepared', revokeObjectURL() {} },
    $: id => document.getElementById(id), headers: () => ({}), qs: value => value,
    showBuilderMsg() {}, flushDeferredBuilderState() {}, video, button,
    fetch: (_url, { signal } = {}) => new Promise((resolve, fail) => {
      respond = resolve; reject = fail;
      signal?.addEventListener('abort', () => fail(new Error('aborted')));
    }),
  });
  vm.runInContext(pageSource.slice(pageSource.indexOf('const SHARE_TEXT ='), pageSource.indexOf('let submitBusy =')), context);
  return { ...h, button, resolve: () => respond({ ok: true, blob: async () => new Blob(['video'], { type: 'video/mp4' }) }),
    reject: () => reject(new Error('offline')), shared: () => shared };
}

test('requested sharing blocks new edits through preparation and its second tap, then releases before the native sheet resolves', async () => {
  const h = sharingSetup(), { context, ops, send, button } = h;
  vm.runInContext("videoSharePreparation = prepareVideoShare('/video.mp4', video)", context);
  const request = context.sharePreparedVideo('/video.mp4', button);
  assert(ops.isBusy()); assert(send('#title', 'beforeinput'));
  h.resolve(); await request;
  assert(ops.isBusy(), 'waiting for the required second tap is still the same action');
  assert.equal(button.textContent, '共有を開く');
  await context.sharePreparedVideo('/video.mp4', button);
  assert.equal(h.shared(), 1);
  assert(!ops.isBusy(), 'an unresolved iOS share Promise must not retain the global lock');
});

test('failed or canceled preparation releases sharing and late completion cannot open its sheet', async () => {
  for (const cancel of [false, true]) {
    const h = sharingSetup(), { context, document, ops, button } = h;
    vm.runInContext("videoSharePreparation = prepareVideoShare('/video.mp4', video)", context);
    const request = context.sharePreparedVideo('/video.mp4', button);
    if (cancel) { document.querySelector('.operation-cancel').click(); h.resolve(); }
    else h.reject();
    await request;
    assert(!ops.isBusy()); assert.equal(h.shared(), 0);
    assert(!ops.hasAction(button));
  }
});

for (const name of ['shareHistoryResult', 'saveHistoryVideo']) {
  test(`${name} uses the same preparation, second-tap and cancellation lifetime`, async () => {
    const h = sharingSetup(), { context, ops, document, button } = h;
    Object.assign(context, { historyJobs: [{ id: 'job', video_url: '/video.mp4' }],
      preparedHistorySave: null, preparedHistoryShare: null });
    vm.runInContext(pageFunction('resetHistoryShare') + pageFunction('resetHistorySave') + pageFunction(name), context);
    context.navigator.platform = 'iPhone';
    const status = document.createElement('p'); document.body.append(status);
    const request = context[name](0, button, status);
    assert(ops.isBusy());
    h.resolve(); await request;
    assert(ops.isBusy(), 'the ready continuation stays available while new starts are blocked');
    await context[name](0, button, status);
    assert.equal(h.shared(), 1);
    assert(!ops.isBusy());
  });
}

test('preparation timeout releases the global restriction without opening the native sheet', async () => {
  const h = sharingSetup(), { context, ops, button } = h;
  let timeout;
  context.setTimeout = (handler, ms) => { if (ms > 0) { assert(ms <= 30000); timeout = handler; } return 1; };
  context.clearTimeout = () => {};
  vm.runInContext("videoSharePreparation = prepareVideoShare('/video.mp4', video)", context);
  const request = context.sharePreparedVideo('/video.mp4', button);
  assert(ops.isBusy());
  timeout(); await request;
  assert(!ops.isBusy()); assert.equal(h.shared(), 0);
});

test('cancel remains outside a closed save menu and owner removal releases the action', async () => {
  const { document, ops } = setup();
  const menu = document.createElement('details');
  const summary = document.createElement('summary'); menu.append(summary); document.body.append(menu);
  let canceled = 0;
  ops.beginAction(summary, { onCancel: () => canceled++ });
  assert.equal(document.querySelector('.operation-cancel').parentElement, menu.parentElement,
    'a closed details element must not hide cancellation');
  menu.remove();
  await new Promise(resolve => setImmediate(resolve));
  assert(!ops.isBusy()); assert.equal(canceled, 1);
});

test('cancellation retains ownership until asynchronous cleanup is settled', async () => {
  const { document, ops } = setup();
  let settle;
  const cleanup = new Promise(resolve => { settle = resolve; });
  const action = ops.beginAction(document.querySelector('#continue'), { onCancel: () => cleanup });
  document.querySelector('.operation-cancel').click();
  assert(action.canceled); assert(ops.isBusy());
  assert(document.querySelector('.operation-cancel').disabled);
  settle(); await cleanup; await new Promise(resolve => setImmediate(resolve));
  assert(!ops.isBusy());
});

test('a committed import cannot be canceled halfway through application', () => {
  const { document, ops } = setup();
  const action = ops.beginAction(document.querySelector('#continue'));
  action.setCancelable(false); action.cancel();
  assert(!action.canceled); assert(ops.isBusy());
  action.finish(); assert(!ops.isBusy());
});

test('review presentation can exclude its own foreground scope but not another operation', () => {
  const { ops, context } = setup();
  vm.runInContext(pageFunction('reviewPresentationBlocked'), context);
  const owner = ops.begin();
  assert(!context.reviewPresentationBlocked(owner));
  assert(context.reviewPresentationBlocked());
  const other = ops.begin();
  assert(context.reviewPresentationBlocked(owner));
  other.end(); owner.end();
});

function bridgeSetup(receiving = false) {
  const h = setup(), { context } = h;
  const nonce = '00000000-0000-4000-8000-000000000001';
  const sent = [], listeners = new Map(), timers = new Set();
  const peer = { closed: false, postMessage: (data, origin) => sent.push({ data, origin }) };
  const location = { origin: 'https://video.test', href: 'https://video.test/' + (receiving ? '?lyrics-transfer=' + nonce : '') };
  const window = {
    opener: peer, open: () => peer,
    addEventListener: (type, fn) => listeners.set(type, fn),
    removeEventListener: (type, fn) => { if (listeners.get(type) === fn) listeners.delete(type); },
  };
  Object.assign(context, { window, location, Blob, URL, DOMException,
    crypto: { randomUUID: () => nonce }, history: { replaceState() {} },
    setTimeout: fn => { timers.add(fn); return fn; }, clearTimeout: fn => timers.delete(fn),
    setInterval: fn => { timers.add(fn); return fn; }, clearInterval: fn => timers.delete(fn),
  });
  vm.runInContext(readFileSync(new URL('../src/soramimic_video/static/app-bridge.js', import.meta.url), 'utf8'), context);
  const message = (action, payload) => listeners.get('message')?.({
    source: peer, origin: 'https://editor.test',
    data: { type: 'soramimic:lyrics-transfer/1', nonce, action, payload },
  });
  return { ...h, bridge: window.SoramimicBridge, peer, sent, message, timers,
    payload: { results: [[{}]], unitsList: [[{}]], lyrics: 'test' } };
}

test('canceling an outbound transfer cleans listeners and ignores data which arrives late', async () => {
  const h = bridgeSetup(), ctrl = new AbortController();
  let finish;
  const request = h.bridge.send('https://editor.test/editor.html', () => new Promise(resolve => { finish = resolve; }), { signal: ctrl.signal });
  const ready = h.message('ready');
  ctrl.abort();
  await assert.rejects(request, { name: 'AbortError' });
  finish(h.payload); await ready;
  assert(!h.sent.some(message => message.data.action === 'data'), 'late data must not be sent');
  assert.equal(h.sent.at(-1).data.action, 'error', 'the receiver must be told to stop waiting');
  assert.equal(h.timers.size, 0);
});

test('canceling an inbound transfer prevents late application', async () => {
  const h = bridgeSetup(true), ctrl = new AbortController();
  let applications = 0;
  const request = h.bridge.receive('https://editor.test/editor.html', () => { applications++; }, { signal: ctrl.signal });
  ctrl.abort(); await assert.rejects(request, { name: 'AbortError' });
  await h.message('data', h.payload);
  assert.equal(applications, 0); assert.equal(h.timers.size, 0);
  assert.equal(h.sent.at(-1).data.action, 'error', 'the sender must be told to stop waiting');
});

test('an inbound transfer finishes its committed application before releasing the request', async () => {
  const h = bridgeSetup(true), ctrl = new AbortController();
  let finish, applying = false, settled = false;
  const request = h.bridge.receive('https://editor.test/editor.html', () => new Promise(resolve => { finish = resolve; }),
    { signal: ctrl.signal, onApplying: () => { applying = true; } });
  request.then(() => { settled = true; });
  const arrival = h.message('data', h.payload);
  assert(applying); ctrl.abort(); await Promise.resolve();
  assert(!settled, 'a cancellation must not expose partially applied input');
  finish(); await arrival; await request;
  assert.equal(h.sent.at(-1).data.action, 'accepted');
  assert.equal(h.timers.size, 0);
});


test('sender cleanup survives failure to notify a disconnected peer', async () => {
  const h = bridgeSetup();
  h.peer.postMessage = () => { throw new Error('peer gone'); };
  const request = h.bridge.send('https://editor.test/editor.html', async () => { throw new Error('read failed'); });
  const failure = assert.rejects(request, /read failed/);
  await h.message('ready'); await failure;
  assert.equal(h.timers.size, 0);
});

test('receiver cleanup survives failure to notify a disconnected peer', async () => {
  const h = bridgeSetup(true);
  const request = h.bridge.receive('https://editor.test/editor.html', async () => { throw new Error('apply failed'); });
  h.peer.postMessage = () => { throw new Error('peer gone'); };
  const failure = assert.rejects(request, /apply failed/);
  await h.message('data', h.payload); await failure;
  assert.equal(h.timers.size, 0);
});

function historyPlaybackSetup(play) {
  const h = setup(), { document, context } = h;
  const card = document.createElement('article'); card.className = 'history-item';
  const slot = document.createElement('div'); slot.className = 'history-media';
  const thumb = document.createElement('button'); thumb.className = 'history-thumb';
  card.append(slot); slot.append(thumb); document.body.append(card);
  const timers = new Set(), create = document.createElement.bind(document);
  let pause = () => {};
  document.createElement = tag => {
    const element = create(tag);
    if (['video', 'audio'].includes(tag)) {
      element.play = () => play(element);
      element.pause = () => pause();
      element.load = () => {};
    }
    return element;
  };
  Object.assign(context, {
    AbortController, headers: () => ({}), qs: value => value, apiKey: () => '',
    historyJobs: [{ id: 'done', playback_url: '/playback', thumbnail_url: '/thumbnail' }],
    resetHistorySave() {}, resetHistoryShare() {},
    setTimeout: fn => { timers.add(fn); return fn; }, clearTimeout: fn => timers.delete(fn),
    URL: { revokeObjectURL() {} },
  });
  vm.runInContext('let activeHistoryPlayback = null;\n'
    + pageFunction('disposeHistoryPlayback') + '\n' + pageFunction('startHistoryPlayback'), context);
  return { ...h, slot, thumb, timers, play: fn => { play = fn; }, pause: fn => { pause = fn; } };
}

test('failed history media restores the replay control and permits a successful retry', async () => {
  const h = historyPlaybackSetup(() => Promise.reject(Object.assign(new Error('unavailable'), { name: 'NotSupportedError' })));
  await h.context.startHistoryPlayback(0, h.thumb);
  assert(h.slot.querySelector('.history-thumb') === h.thumb, 'the original replay button is restored');
  assert.match(h.slot.querySelector('.history-player-msg').textContent, /もう一度/);
  assert(!h.ops.isBusy());
  h.play(() => Promise.resolve());
  await h.context.startHistoryPlayback(0, h.thumb);
  assert(h.slot.querySelector('video'));
  assert(!h.slot.querySelector('.history-player-msg'), 'retry replaces the old failure message');
  assert(!h.ops.isBusy());
});

test('autoplay refusal retains usable native controls without reporting a data failure', async () => {
  const h = historyPlaybackSetup(() => Promise.reject(Object.assign(new Error('activation required'), { name: 'NotAllowedError' })));
  await h.context.startHistoryPlayback(0, h.thumb);
  assert(h.slot.querySelector('video').controls);
  assert(!h.slot.querySelector('.history-player-msg'));
  assert(!h.ops.isBusy());
});

test('history preparation timeout restores replay without unlocking unrelated work', async () => {
  let reject;
  const h = historyPlaybackSetup(() => new Promise((_, fail) => { reject = fail; }));
  h.pause(() => reject(Object.assign(new Error('paused'), { name: 'AbortError' })));
  const unrelated = h.ops.begin();
  const pending = h.context.startHistoryPlayback(0, h.thumb);
  [...h.timers][0]();
  await pending;
  assert(h.slot.querySelector('.history-thumb') === h.thumb, 'the original replay button is restored');
  assert(h.slot.querySelector('.history-player-msg'));
  assert(h.ops.isBusy(), 'only the failed player releases its restriction');
  unrelated.end(); assert(!h.ops.isBusy());
});
