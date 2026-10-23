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
  const context = { document, Element, MutationObserver };
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
