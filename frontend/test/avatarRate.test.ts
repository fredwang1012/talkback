import { describe, expect, it } from "vitest";
import { AVATAR_PER_MIN, SpendTracker } from "../src/spend";

describe("avatar price by output size", () => {
  it("640x640 is $0.09/min; 480x640 and 640x480 are $0.07/min", () => {
    expect(AVATAR_PER_MIN).toEqual({ "640x640": 0.09, "480x640": 0.07, "640x480": 0.07 });
  });

  it("the tracker prices fresh renders at the configured size, square until told otherwise", () => {
    const t = new SpendTracker();
    t.addAvatar(60);
    expect(t.snapshot().usd).toBeCloseTo(0.09, 6);
    t.setAvatarSize("480x640");
    expect(t.snapshot().usd).toBeCloseTo(0.07, 6);
    t.setAvatarSize("nonsense");
    expect(t.snapshot().usd).toBeCloseTo(0.09, 6);
  });
});
