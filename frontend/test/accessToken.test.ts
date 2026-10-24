import { beforeEach, describe, expect, it } from "vitest";
import { getAccessToken, setAccessToken } from "../src/accessToken";
import { authHeaders } from "../src/api";

const store = new Map<string, string>();
beforeEach(() => {
  store.clear();
  (globalThis as { sessionStorage?: unknown }).sessionStorage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
  };
});

describe("access code", () => {
  it("is empty until the visitor enters it, then sent as X-Talkback-Token", () => {
    expect(authHeaders()).toEqual({});
    setAccessToken("letmein");
    expect(getAccessToken()).toBe("letmein");
    expect(authHeaders()).toEqual({ "X-Talkback-Token": "letmein" });
    setAccessToken("");
    expect(authHeaders()).toEqual({});
  });

  it("survives blocked storage without throwing", () => {
    (globalThis as { sessionStorage?: unknown }).sessionStorage = {
      getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); }, removeItem: () => { throw new Error("blocked"); },
    };
    expect(getAccessToken()).toBe("");
    expect(() => setAccessToken("x")).not.toThrow();
  });

  it("is never read from a build-time env var", async () => {
    const src = await import("node:fs").then((fs) => fs.readFileSync(new URL("../src/api.ts", import.meta.url), "utf8"));
    expect(src).not.toMatch(/import\.meta\.env/);
  });
});
