import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.value = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, handler) { this.events[name] = handler; }
  focus() { this.focused = true; }
  select() { this.selected = true; }
  all(tag) { return this.children.flatMap(child => child instanceof Element
    ? [...(child.tag === tag ? [child] : []), ...child.all(tag)] : []); }
}
const document = {createElement: tag => new Element(tag)};
let clipboard = '';
const navigator = {clipboard: {writeText: async text => { clipboard = text; }}};
const window = {};
vm.runInNewContext(fs.readFileSync('src/soramimic_video/static/image-credits.js', 'utf8'),
  {document, navigator, window, URL});
const {safeUrl, render} = window.ImageCredits;
for (const value of ['', 'javascript:alert(1)', 'data:text/html,x', '/relative', 'https://[bad']) {
  assert.equal(safeUrl(value), '');
}
assert.equal(safeUrl('https://youtu.be/a?t=42'), 'https://youtu.be/a?t=42');
const root = new Element('div');
let saved = 0;
const text = 'A & B\n非公式クレジット\nhttps://youtu.be/a?t=42\nhttps://note.com/a';
render(root, [
  {original: 'A <script>', org: '所属A', image_credit: '非公式クレジット',
    image_page: 'https://youtu.be/a?t=42', image_terms_page: 'https://note.com/a'},
  {original: 'B', image_page: 'javascript:alert(1)'},
], {text, download: async () => { saved++; }});
assert.equal(root.all('article').length, 2);
assert.equal(root.all('a').length, 2);
assert.equal(root.all('script').length, 0);
assert.equal(root.all('h3')[0].textContent, 'A <script>');
const input = root.all('input')[0];
input.value = '所属A';
input.events.input();
assert.equal(root.all('article').length, 1);
assert.equal(root.all('p')[0].textContent, '使用素材：2件（表示 1件）');
const [copy, save] = root.all('button');
await copy.events.click();
assert.equal(clipboard, text); // Search does not truncate the exported credits.
await save.events.click();
assert.equal(saved, 1);
assert.equal(save.disabled, false);
navigator.clipboard.writeText = async () => { throw new Error('unavailable'); };
await copy.events.click();
assert.equal(root.all('textarea')[0].selected, true);
input.value = '存在しない';
input.events.input();
assert.equal(root.all('article').length, 0);
assert.equal(root.all('p').at(-1).textContent, '該当する素材はありません。');
render(root, [], {text: ''});
assert.equal(root.all('article').length, 0);
assert.equal(root.all('p')[0].textContent, '使用素材：0件（表示 0件）');
console.log('Image credits search, safe links, copy, fallback, and download passed');
