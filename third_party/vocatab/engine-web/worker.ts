// ブラウザ版のエンジンの Web Worker（ui/dist/engine-worker.js になる）。本体は core.ts。
// 重い処理で画面が止まらないよう、画面とは別のスレッドで動かし、client.ts とメッセージでやり取りする。
import { Engine } from "./core";
import type { SynthResp, WorkerMsg, WorkerReply } from "./protocol";

declare const ort: any; // onnxruntime-web（init で importScripts して入れる）
declare function importScripts(...urls: string[]): void;

const ORT_VERSION = "1.20.1";
const ORT_BASE = `https://cdn.jsdelivr.net/npm/onnxruntime-web@${ORT_VERSION}/dist/`;

const post = (m: WorkerReply, transfer: Transferable[] = []): void => (self as any).postMessage(m, transfer);
let engine: Engine | null = null;

/**
 * onnxruntime-web の読み込み。2 種類の配布物がある:
 *   ort.webgpu.min.js  WebGPU 入り。中の WASM（jsep 版、約 22 MB）は非同期の呼び出しのために関数が大きく、
 *                      WebKit（Safari 26.2 以降）ではその JIT コンパイルが暴走して CPU と数 GB のメモリを食い、
 *                      ページがメモリ不足で読み込み直される（WebKit bug 304810、onnxruntime issue #26827）。
 *                      GPU を使わない設定（executionProviders: ["wasm"]）でも同じ WASM を読むので、同じ問題が起きる
 *   ort.wasm.min.js    CPU（WASM）だけ。中の WASM は約 11 MB で、上の問題が無い。計算の部品は同じなので音は変わらない
 * なので GPU を使う（頼まれていて、navigator.gpu があり、WebKit ではない）ときだけ WebGPU 入りを読み、ほかは CPU だけの版にする。
 */
function loadOrt(useGpu: boolean): boolean {
  const webkit = /AppleWebKit/.test(navigator.userAgent) && !/Chrome|Chromium|Edg\//.test(navigator.userAgent);
  const gpu = useGpu && !!(navigator as any).gpu && !webkit;
  importScripts(`${ORT_BASE}${gpu ? "ort.webgpu.min.js" : "ort.wasm.min.js"}`);
  ort.env.wasm.wasmPaths = ORT_BASE;
  // 複数のスレッドは cross-origin isolated のときだけ使える（GitHub Pages では普通は 1 スレッド）
  ort.env.wasm.numThreads = (self as any).crossOriginIsolated ? Math.min(4, navigator.hardwareConcurrency || 1) : 1;
  return gpu;
}

(self as any).onmessage = async (e: MessageEvent<WorkerMsg>) => {
  const m = e.data;
  try {
    if (m.type === "init") {
      const gpu = loadOrt(m.gpu);
      engine = new Engine(ort, (loaded, total) => post({ type: "progress", loaded, total }));
      post({ type: "result", id: m.id, result: await engine.init(m.base, true, gpu) });
    } else if (!engine) {
      throw new Error("エンジンがまだ準備できていません");
    } else if (m.type === "analyze") {
      post({ type: "result", id: m.id, result: await engine.analyze(m.req) });
    } else if (m.type === "synthesize") {
      const res: SynthResp = await engine.synthesize(m.req);
      // 大きい配列は写さずに渡す（どれも合成のたびに新しく作ったもので、エンジン側には残らない）
      const tr = [res.wav, res.mel_in, res.mel_out, res.mel_plain].filter((a): a is Float32Array => !!a).map((a) => a.buffer);
      post({ type: "result", id: m.id, result: res }, tr);
    } else if (m.type === "restore") {
      // 待機から起こされた直後: 画面が持っている解析の結果を取り置きへ戻す（client.ts の wake）
      post({ type: "result", id: m.id, result: { restored: engine.restore(m.entries) } });
    } else if (m.type === "breaths") {
      post({ type: "result", id: m.id, result: await engine.breaths(m.voicebank) });
    } else if (m.type === "breath") {
      const res = await engine.breath(m.req);
      post({ type: "result", id: m.id, result: res }, [res.wav.buffer]);
    }
  } catch (err) {
    post({ type: "error", id: m.id, error: err instanceof Error ? err.message : String(err) });
  }
};
