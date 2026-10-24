import { describe, expect, it } from "vitest";
import { AudioClock, DriftController } from "../src/talk/driftController";

describe("DriftController", () => {
  it("does nothing inside the dead-band", () => {
    const c = new DriftController();
    for (let t = 0; t < 10; t++) expect(c.update(1.03, 1, t * 100).kind).toBe("none");
  });
  it("nudges playbackRate for small drift, clamped, in the right direction", () => {
    const c = new DriftController();
    let a = c.update(1.15, 1, 0); // video ahead -> slower
    expect(a.kind).toBe("rate");
    expect(a.rate).toBeLessThan(1);
    expect(a.rate).toBeGreaterThanOrEqual(0.92);
    const c2 = new DriftController();
    a = c2.update(0.8, 1, 0); // behind -> faster
    expect(a.rate).toBeGreaterThan(1);
    expect(a.rate).toBeLessThanOrEqual(1.08);
  });
  it("returns the rate to 1 once back in the dead-band", () => {
    const c = new DriftController({ emaAlpha: 1 });
    c.update(1.2, 1, 0);
    expect(c.update(1.0, 1, 100)).toEqual({ kind: "rate", rate: 1 });
  });
  it("only seeks above the hard threshold and respects the cooldown", () => {
    const c = new DriftController({ emaAlpha: 1 });
    expect(c.update(1.25, 1, 0).kind).toBe("rate"); // 250 ms: no seek
    const s = c.update(2, 1, 100);
    expect(s).toEqual({ kind: "seek", to: 1, rate: 1 });
    expect(c.update(3, 1, 600).kind).not.toBe("seek"); // within cooldown
    expect(c.update(3, 1, 1200).kind).toBe("seek");
  });
  it("smooths a single noisy sample instead of seeking", () => {
    const c = new DriftController();
    c.update(1.0, 1, 0);
    c.update(1.0, 1, 100);
    expect(c.update(1.5, 1, 200).kind).not.toBe("seek"); // one spike is averaged away
  });
  it("applies a constant start offset", () => {
    const c = new DriftController({ startOffsetS: 0.1 });
    expect(c.update(1.1, 1, 0).kind).toBe("none");
    const s = new DriftController({ startOffsetS: 0.1, emaAlpha: 1 }).update(5, 1, 0);
    expect(s).toEqual({ kind: "seek", to: 1.1, rate: 1 });
  });
});

describe("AudioClock", () => {
  it("advances with the wall clock and ignores coarse readings", () => {
    const k = new AudioClock();
    k.anchor(0, 1000);
    expect(k.sample(0.4, 1500)).toBeGreaterThan(0.45); // reading is 100 ms stale but the clock knows better
    expect(k.sample(0.5, 1500)).toBeCloseTo(0.5, 1);
  });
  it("re-anchors after a real jump and freezes while paused", () => {
    const k = new AudioClock();
    k.anchor(0, 0);
    expect(k.sample(5, 1000)).toBeCloseTo(5, 5);
    k.pause(1000);
    expect(k.position(5000)).toBeCloseTo(5, 5);
    k.resume(5000);
    expect(k.position(5500)).toBeCloseTo(5.5, 5);
  });
});
