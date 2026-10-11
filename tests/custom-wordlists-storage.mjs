import assert from 'node:assert/strict';
import test from 'node:test';
import { IDBFactory, IDBObjectStore } from 'fake-indexeddb';
import '../src/soramimic_video/static/custom-wordlists.js';

const key = 'soramimic-video-custom-wordlists';
const createRepository = globalThis.VideoCustomWordlists.createRepository;
function fixture(raw = null) {
  const indexedDB = new IDBFactory();
  const source = raw;
  const legacyStorage = {
    getItem: (name) => { assert.equal(name, key); return source; },
    setItem: () => assert.fail('Legacy data must not be overwritten'),
    removeItem: () => assert.fail('Legacy data must remain recoverable'),
  };
  return { indexedDB, legacyStorage, repository: () => createRepository({ indexedDB, legacyStorage }) };
}
const legacyList = { id: 'old', name: '旧リスト', text: 'word\t単語', createdAt: '2020-01-01', updatedAt: '2020-01-02' };

test('committed lists survive repository reload; edits preserve identity, date and order', async () => {
  const f = fixture();
  const first = await f.repository().save({ name: '  First  ', text: '  one  ' });
  const second = await f.repository().save({ name: 'Second', text: 'two' });
  const edited = await f.repository().save({ id: first.id, name: 'Edited', text: 'three' });
  assert.equal(edited.id, first.id);
  assert.equal(edited.createdAt, first.createdAt);
  assert.deepEqual(await f.repository().lists(), [edited, second]);
});

test('text larger than 1 MB and 10 MB survives saving and reload without truncation', async () => {
  const f = fixture();
  for (const size of [1024 * 1024 + 1, 10 * 1024 * 1024 + 1]) {
    const text = 'a'.repeat(size);
    const saved = await f.repository().save({ name: String(size), text });
    assert.equal((await f.repository().lists()).find((list) => list.id === saved.id).text, text);
  }
});

test('migration preserves all fields and order, and deletion never reimports old data', async () => {
  const lists = [legacyList, { ...legacyList, id: 'other', extra: 'preserved' }];
  const raw = JSON.stringify({ version: 1, lists });
  const f = fixture(raw);
  assert.deepEqual(await f.repository().lists(), lists);
  await f.repository().remove('old');
  await f.repository().remove('other');
  assert.deepEqual(await f.repository().lists(), []);
  assert.equal(f.legacyStorage.getItem(key), raw);
  await assert.rejects(f.repository().save({ id: 'old', name: 'Stale', text: 'data' }), /削除されています/);
  assert.deepEqual(await f.repository().lists(), []);
});

test('concurrent tabs serialize migration and additions without losing a list', async () => {
  const f = fixture(JSON.stringify({ version: 1, lists: [legacyList] }));
  const saved = await Promise.all(Array.from({ length: 25 }, (_, index) =>
    f.repository().save({ name: `List ${index}`, text: `word ${index}` })));
  const lists = await f.repository().lists();
  assert.equal(lists.length, 26);
  assert.equal(lists[0].id, legacyList.id);
  assert.deepEqual(new Set(lists.slice(1).map((list) => list.id)), new Set(saved.map((list) => list.id)));
  const repository = f.repository();
  const deletion = repository.remove(saved[0].id);
  const staleEdit = repository.save({ id: saved[0].id, name: 'stale', text: 'stale' });
  await deletion;
  await assert.rejects(staleEdit, /削除されています/);
});

test('invalid input never opens or writes a database or reads legacy storage', async () => {
  const repository = createRepository({
    indexedDB: { open: () => assert.fail('No database writes before validation') },
    legacyStorage: { getItem: () => assert.fail('No migration before validation') },
  });
  for (const input of [{ name: '', text: 'word' }, { name: 'x'.repeat(101), text: 'word' }, { name: 'name', text: '  ' }]) {
    await assert.rejects(repository.save(input), /入力|100文字/);
  }
});

test('corrupt legacy storage is preserved and blocks mutation', async () => {
  for (const raw of ['broken', '{}', JSON.stringify({ version: 2, lists: [] }),
    JSON.stringify({ version: 1, lists: [legacyList, legacyList] }),
    JSON.stringify({ version: 1, lists: [{ id: 'bad', name: 'bad', text: 1 }] })]) {
    const f = fixture(raw);
    await assert.rejects(f.repository().lists(), /読み取れません/);
    await assert.rejects(f.repository().save({ name: 'new', text: 'new' }), /読み取れません/);
    assert.equal(f.legacyStorage.getItem(key), raw);
  }
});

test('failed migration retains its source and retries on the next operation', async () => {
  const raw = JSON.stringify({ version: 1, lists: [legacyList] });
  const f = fixture(raw);
  const originalPut = IDBObjectStore.prototype.put;
  IDBObjectStore.prototype.put = function () { throw new DOMException('full', 'QuotaExceededError'); };
  try { await assert.rejects(f.repository().lists(), /空き容量/); }
  finally { IDBObjectStore.prototype.put = originalPut; }
  assert.equal(f.legacyStorage.getItem(key), raw);
  assert.deepEqual(await f.repository().lists(), [legacyList]);
});

test('transaction abort after put rejects and preserves previously committed data', async () => {
  const f = fixture();
  const repository = f.repository();
  const saved = await repository.save({ name: 'Original', text: 'original' });
  const originalPut = IDBObjectStore.prototype.put;
  IDBObjectStore.prototype.put = function (...args) {
    const request = originalPut.apply(this, args);
    request.addEventListener('success', () => this.transaction.abort());
    return request;
  };
  try { await assert.rejects(repository.save({ id: saved.id, name: 'Replacement', text: 'replacement' }), /保存領域/); }
  finally { IDBObjectStore.prototype.put = originalPut; }
  assert.deepEqual(await f.repository().lists(), [saved]);
});

test('migration and its first mutation roll back together on abort', async () => {
  const raw = JSON.stringify({ version: 1, lists: [legacyList] });
  const f = fixture(raw);
  const originalPut = IDBObjectStore.prototype.put;
  IDBObjectStore.prototype.put = function (...args) {
    const request = originalPut.apply(this, args);
    request.addEventListener('success', () => this.transaction.abort());
    return request;
  };
  try { await assert.rejects(f.repository().remove('old'), /保存領域/); }
  finally { IDBObjectStore.prototype.put = originalPut; }
  assert.equal(f.legacyStorage.getItem(key), raw);
  assert.deepEqual(await f.repository().lists(), [legacyList]);
});

test('unavailable, blocked, and inaccessible storage produce useful errors', async () => {
  await assert.rejects(createRepository({ indexedDB: null }).lists(), /保存設定/);
  const factory = { open: () => {
    const request = {};
    queueMicrotask(() => request.onblocked());
    return request;
  } };
  await assert.rejects(createRepository({ indexedDB: factory }).lists(), /ほかのタブ/);
  const f = fixture();
  await assert.rejects(createRepository({ indexedDB: f.indexedDB, legacyStorage: {
    getItem: () => { throw new DOMException('denied', 'SecurityError'); },
  } }).lists(), /保存設定/);
});

test('a stale edit during first migration leaves the legacy source recoverable', async () => {
  const raw = JSON.stringify({ version: 1, lists: [legacyList] });
  const f = fixture(raw);
  await assert.rejects(f.repository().save({ id: 'missing', name: 'Missing', text: 'word' }), /削除されています/);
  assert.equal(f.legacyStorage.getItem(key), raw);
  assert.deepEqual(await f.repository().lists(), [legacyList]);
});

test('changing returned data cannot alter committed storage', async () => {
  const f = fixture();
  const saved = await f.repository().save({ name: 'Original', text: 'word' });
  const id = saved.id;
  saved.name = 'Mutated';
  const lists = await f.repository().lists();
  assert.equal(lists[0].name, 'Original');
  lists[0].text = 'Mutated';
  lists.push({ id: 'injected', name: 'injected', text: 'injected' });
  assert.deepEqual((await f.repository().lists()).map(({ id, name, text }) => ({ id, name, text })),
    [{ id, name: 'Original', text: 'word' }]);
});

test('a denied localStorage getter rejects asynchronously; completed migration no longer reads it', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    get: () => { throw new DOMException('denied', 'SecurityError'); },
  });
  try {
    const f = fixture();
    const repository = createRepository({ indexedDB: f.indexedDB });
    await assert.rejects(repository.lists(), /保存設定/);
    const saved = await f.repository().save({ name: 'Saved', text: 'word' });
    assert.deepEqual(await repository.lists(), [saved]);
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor);
    else delete globalThis.localStorage;
  }
});
