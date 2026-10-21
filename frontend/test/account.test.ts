import { describe, expect, it } from "vitest";
import { accountLabel, SIGN_OUT_URL } from "../src/api";

describe("account label", () => {
  it("shows the email only when per_user and signed in", () => {
    expect(accountLabel({ email: "a@b.com", per_user: true })).toBe("a@b.com");
    expect(accountLabel({ email: null, per_user: true })).toBeNull();
    expect(accountLabel({ email: "a@b.com", per_user: false })).toBeNull();
    expect(accountLabel(null)).toBeNull();
  });
  it("signs out through IAP", () => {
    expect(SIGN_OUT_URL).toContain("gcp-iap-mode=CLEAR_LOGIN_COOKIE");
  });
});
