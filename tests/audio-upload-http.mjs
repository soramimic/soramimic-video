// Exercise the real API over TCP, including a request cut in the middle of its body.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {File} from 'node:buffer';
import http from 'node:http';
import vm from 'node:vm';

const [base, path] = process.argv.slice(2);
let cookie = '', cutBody = false, lostChunkAck = false, lostJobAck = false, putCount = 0;
const offsets = [], statuses = [];
const context = vm.createContext({FormData, URLSearchParams, AbortController, DOMException,
  setTimeout(callback, delay) {
    if (delay < 20000) {queueMicrotask(callback); return 0;}
    return setTimeout(callback, delay);
  }, clearTimeout,
  async fetch(url, options) {
    options.headers = {...options.headers, ...(cookie ? {Cookie: cookie} : {})};
    if (options.method === 'PUT') {
      putCount++;
      const offset = Number(new URL(url, base).searchParams.get('offset'));
      offsets.push(offset);
      if (!cutBody) {
        cutBody = true;
        const data = Buffer.from(await options.body.arrayBuffer());
        await new Promise((resolve, reject) => {
          const request = http.request(new URL(url, base), {method: 'PUT',
            headers: {...options.headers, 'Content-Length': data.length}}, resolve);
          request.on('error', reject);
          request.write(data.subarray(0, Math.floor(data.length / 2)));
          setTimeout(() => request.destroy(new Error('simulated mobile disconnection')), 30);
        });
        assert.fail('the incomplete request must fail');
      }
    }
    const result = await fetch(new URL(url, base), options);
    const issued = result.headers.get('set-cookie');
    if (issued) cookie = issued.split(';')[0];
    if (options.method === 'PUT' && result.ok && !lostChunkAck) {
      lostChunkAck = true;
      await result.arrayBuffer();
      throw new TypeError('lost acknowledgement after committed chunk');
    }
    if (url === '/api/jobs' && result.ok && !lostJobAck) {
      lostJobAck = true;
      await result.arrayBuffer();
      throw new TypeError('lost acknowledgement after job creation');
    }
    return result;
  }, console});
vm.runInContext(readFileSync('src/soramimic_video/static/audio-upload.js', 'utf8'), context);
const form = new FormData();
form.set('audio', new File([readFileSync(path)], 'four-minutes.mp3'));
form.set('submission_id', 'f'.repeat(32));
form.set('turnstile_token', 'once');
form.set('wordlist', 'stations');
const result = await context.SoramimicAudioUpload.send(form, {
  headers: () => ({}), signal: new AbortController().signal,
  onStatus: text => statuses.push(text),
});
assert.equal(result.status, 200);
const {id} = await result.json();
assert.equal(typeof id, 'string');
assert.ok(cutBody && lostChunkAck && lostJobAck);
assert.deepEqual(offsets.slice(0, 3), [0, 0, 32768]);
assert.ok(statuses.some(text => text.includes('届いた部分')));
assert.ok(putCount > 3);
console.log(JSON.stringify({job_id: id, chunks: putCount, resumed: true}));
