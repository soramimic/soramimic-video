import { build } from 'esbuild';
import { readFile, writeFile } from 'node:fs/promises';
const target = new URL('../src/soramimic_video/static/score-worker.js', import.meta.url);
const result = await build({
 entryPoints: [new URL('../third_party/vocatab/engine-web/worker.ts', import.meta.url).pathname],
 bundle: true, write: false, format: 'iife', platform: 'browser', target: 'es2020', minify: true,
 banner: {js: '/*! VocaTab browser engine, Copyright (c) 2026 wavtechyukky. MIT / Apache-2.0. See /score-engine-notices.txt. */'},
});
const code = result.outputFiles[0].text;
if (process.argv.includes('--check')) {
 if (await readFile(target, 'utf8') !== code) throw new Error('Rebuild score-worker.js with npm run build:score');
} else await writeFile(target, code);
