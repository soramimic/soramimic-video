import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Element {
  constructor(tag) { this.tag = tag; this.children = []; }
  append(...items) { this.children.push(...items); }
  all(tag) { return this.children.flatMap(child => child instanceof Element
    ? [...(child.tag === tag ? [child] : []), ...child.all(tag)] : []); }
}
const document = {createElement: tag => new Element(tag)};
const window = {};
vm.runInNewContext(fs.readFileSync('src/soramimic_video/static/image-credits.js', 'utf8'),
  {document, window, URL});
const {safeUrl, itemCard} = window.ImageCredits;
for (const value of ['', 'javascript:alert(1)', 'data:text/html,x', '/relative', 'https://[bad']) {
  assert.equal(safeUrl(value), '');
}
assert.equal(safeUrl('https://youtu.be/a?t=42'), 'https://youtu.be/a?t=42');
const card = itemCard({
  original: 'A <script>', org: '所属A', image_credit: '非公式クレジット',
  image_page: 'https://youtu.be/a?t=42', image_terms_page: 'https://note.com/a',
});
assert.equal(card.tag, 'article');
assert.equal(card.all('a').length, 2);
assert.equal(card.all('script').length, 0);
assert.equal(card.all('h3')[0].textContent, 'A <script>');
assert.equal(card.all('p')[0].textContent, '所属A');
assert.equal(card.all('p')[1].textContent, 'クレジット: 非公式クレジット');
for (const link of card.all('a')) {
  assert.equal(link.target, '_blank');
  assert.equal(link.rel, 'noopener noreferrer');
}
const unsafe = itemCard({original: 'B', image_page: 'javascript:alert(1)'});
assert.equal(unsafe.all('a').length, 0);
console.log('Image credit cards and safe links passed');
