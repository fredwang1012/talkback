import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MicCapture } from "../src/audio/capture";

let streams: { stopped: boolean }[];
let contexts: { state: string; closed: boolean }[];
let gum: (() => void) | null;

beforeEach(() => {
  streams = [];
  contexts = [];
  gum = null;
  vi.stubGlobal("navigator", {
    mediaDevices: {
      getUserMedia: () => new Promise((resolve) => {
        const s = { stopped: false, getTracks: () => [{ stop: () => { s.stopped = true; } }] };
        streams.push(s);
        gum = () => resolve(s);
      }),
    },
  });
  vi.stubGlobal("URL", { createObjectURL: () => "blob:x", revokeObjectURL: () => {} });
  vi.stubGlobal("Blob", class {});
  vi.stubGlobal("AudioContext", class {
    sampleRate = 48000; state = "running"; closed = false; destination = {};
    audioWorklet = { addModule: async () => {} };
    constructor() { contexts.push(this); }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    async close() { this.closed = true; this.state = "closed"; }
    async resume() {}
  });
  vi.stubGlobal("AudioWorkletNode", class { port = { onmessage: null, close() {} }; connect() {} disconnect() {} });
});
afterEach(() => vi.unstubAllGlobals());

describe("MicCapture start/stop races", () => {
  it("a second start while the first is pending opens only one stream", async () => {
    const mic = new MicCapture(() => {});
    const a = mic.start(), b = mic.start();
    expect(streams.length).toBe(1);
    gum!();
    await Promise.all([a, b]);
    expect(contexts.length).toBe(1);
    expect(mic.active).toBe(true);
    await mic.stop();
    expect(streams[0].stopped).toBe(true);
    expect(contexts[0].closed).toBe(true);
  });

  it("a tap that releases before the mic opens leaves nothing running", async () => {
    const mic = new MicCapture(() => {});
    const a = mic.start();
    await mic.stop();
    gum!();
    await a;
    expect(streams[0].stopped).toBe(true);
    expect(contexts.length).toBe(0);
    expect(mic.active).toBe(false);
  });
});
