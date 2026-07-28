// ブラウザPoC用の最小静的サーバ(空きポートを自動確保)。
// 本家JSエンジン・データJSON・kuromoji辞書・単語リストCSVを配信し、
// 「何バイト転送したか(生 / gzip後)」も記録する。
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '../..');
const SORA = path.join(REPO, 'external/soramimic');
const WL = path.join(REPO, 'external/soramimic-wordlists');
const KURO = path.join(SORA, 'frontend/node_modules/kuromoji');

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.csv': 'text/csv; charset=utf-8',
  '.gz': 'application/octet-stream',
};

// URL接頭辞 → 実ディレクトリ
const ROUTES = [
  ['/lib/', path.join(SORA, 'frontend/src/lib')],
  ['/data/', path.join(SORA, 'data')],
  ['/kuromoji/dict/', path.join(KURO, 'dict')],
  ['/wordlists/', WL],
  ['/out/', path.join(REPO, 'spike/out')],
  ['/', HERE],
];

export const transferLog = [];

function resolvePath(urlPath) {
  if (urlPath === '/kuromoji.js') return path.join(KURO, 'build/kuromoji.js');
  for (const [prefix, dir] of ROUTES) {
    if (urlPath.startsWith(prefix)) {
      const rel = urlPath.slice(prefix.length) || 'index.html';
      const full = path.join(dir, rel);
      if (full.startsWith(dir) && fs.existsSync(full) && fs.statSync(full).isFile()) return full;
    }
  }
  return null;
}

export function start() {
  const server = http.createServer((req, res) => {
    const urlPath = decodeURIComponent(req.url.split('?')[0]);
    const file = resolvePath(urlPath === '/' ? '/index.html' : urlPath);
    if (!file) { res.writeHead(404); res.end('not found'); return; }
    const buf = fs.readFileSync(file);
    const ext = path.extname(file);
    const acceptsGzip = /\bgzip\b/.test(req.headers['accept-encoding'] || '');
    // .gz(kuromoji辞書)は既に圧縮済みなので二重圧縮しない
    const doGzip = acceptsGzip && ext !== '.gz';
    const body = doGzip ? zlib.gzipSync(buf, { level: 6 }) : buf;
    transferLog.push({ url: urlPath, raw: buf.length, sent: body.length, gzipped: doGzip });
    const headers = { 'Content-Type': MIME[ext] || 'application/octet-stream' };
    if (doGzip) headers['Content-Encoding'] = 'gzip';
    res.writeHead(200, headers);
    res.end(body);
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }));
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const { port } = await start();
  console.log(`http://127.0.0.1:${port}/`);
}
