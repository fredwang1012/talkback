import { describe, expect, it } from "vitest";
import source from "../src/audio/worklets/playback-processor.js?raw";

type Proc = {
  size: number;
  port: { onmessage: (e: { data: unknown }) => void; postMessage: (m: unknown) => void };
  process: (inputs: unknown, outputs: Float32Array[][]) => boolean;
};

/** Evaluate the worklet source with minimal AudioWorklet globals and return a processor instance. */
function makeProcessor(): Proc {
  let Cls: (new () => Proc) | null = null;
  class AudioWorkletProcessor {
    port = { onmessage: (_e: { data: unknown }) => {}, postMessage: (_m: unknown) => {} };
  }
  new Function("AudioWorkletProcessor", "registerProcessor", source)(
    AudioWorkletProcessor,
    (_name: string, c: new () => Proc) => { Cls = c; },
  );
  return new Cls!();
}

function send(p: Proc, samples: number[]) {
  p.port.onmessage({ data: { type: "samples", payload: new Int16Array(samples).buffer } });
}

function drain(p: Proc, n: number): number[] {
  const out: number[] = [];
  while (out.length < n) {
    const block = new Float32Array(128);
    p.process([], [[block]]);
    out.push(...block);
  }
  return out.slice(0, n);
}

describe("playback worklet ring buffer", () => {
  it("keeps every sample of a reply longer than the starting buffer (no overwritten middle)", () => {
    const p = makeProcessor();
    const start = p.size;
    const total = start + 24000 * 20; // 20 s more than the starting size, all arriving before playback
    const chunk = 2400;
    for (let i = 0; i < total; i += chunk) {
      send(p, Array.from({ length: Math.min(chunk, total - i) }, (_, k) => ((i + k) % 30000)));
    }
    expect(p.size).toBeGreaterThan(start);
    const out = drain(p, total);
    for (const i of [0, 1, start - 1, start, start + 1, total - 1]) {
      expect(Math.round(out[i] * 32768)).toBe(i % 30000);
    }
  });

  it("keeps order when growing while partly played (wrapped read pointer)", () => {
    const p = makeProcessor();
    const start = p.size;
    const first = start - 1000;
    send(p, Array.from({ length: first }, (_, i) => i % 1000));
    const played = Math.ceil((start - 2000) / 128) * 128; // drain() consumes whole 128-sample render quanta
    drain(p, played); // read pointer near the end of the ring
    const left = first - played;
    send(p, Array.from({ length: start }, (_, i) => (i % 1000) + 1000)); // wraps, then needs growth
    const out = drain(p, left + start).map((x) => Math.round(x * 32768));
    expect(out.slice(0, 3)).toEqual([0, 1, 2].map((k) => (played + k) % 1000));
    expect(out[left]).toBe(1000);
    expect(out[left + start - 1]).toBe(((start - 1) % 1000) + 1000);
  });

  it("flush drops queued audio", () => {
    const p = makeProcessor();
    send(p, [1000, 2000, 3000]);
    p.port.onmessage({ data: { type: "flush" } });
    expect(drain(p, 3)).toEqual([0, 0, 0]);
  });
});
