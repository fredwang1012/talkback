import { beforeEach, describe, expect, it } from "vitest";
import { clearAccessToken, getAccessToken, isAccessTokenRemembered, setAccessToken } from "../src/accessToken";
import { authHeaders } from "../src/api";

const mk = (m: Map<string, string>) => ({
  getItem: (k: string) => m.get(k) ?? null,
  setItem: (k: string, v: string) => void m.set(k, v),
  removeItem: (k: string) => void m.delete(k),
});
const store = new Map<string, string>();
const local = new Map<string, string>();
beforeEach(() => {
  store.clear();
  local.clear();
  (globalThis as { sessionStorage?: unknown }).sessionStorage = mk(store);
  (globalThis as { localStorage?: unknown }).localStorage = mk(local);
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

  it("remembers on this device (localStorage) or for this tab only (sessionStorage)", () => {
    setAccessToken("tab-only");
    expect(store.size).toBe(1);
    expect(local.size).toBe(0);
    expect(isAccessTokenRemembered()).toBe(false);
    setAccessToken("kept", true);
    expect(store.size).toBe(0);
    expect(local.get("talkback:access-code")).toBe("kept");
    expect(isAccessTokenRemembered()).toBe(true);
    expect(getAccessToken()).toBe("kept"); // read falls back to localStorage
    store.set("talkback:access-code", "this-tab");
    expect(getAccessToken()).toBe("this-tab"); // sessionStorage wins
  });

  it("forgets the code everywhere", () => {
    setAccessToken("kept", true);
    store.set("talkback:access-code", "x");
    clearAccessToken();
    expect(getAccessToken()).toBe("");
    expect(store.size + local.size).toBe(0);
  });

  it("survives blocked storage without throwing", () => {
    (globalThis as { sessionStorage?: unknown }).sessionStorage = {
      getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); }, removeItem: () => { throw new Error("blocked"); },
    };
    (globalThis as { localStorage?: unknown }).localStorage = (globalThis as { sessionStorage?: unknown }).sessionStorage;
    expect(getAccessToken()).toBe("");
    expect(() => setAccessToken("x")).not.toThrow();
    expect(() => setAccessToken("x", true)).not.toThrow();
    expect(() => clearAccessToken()).not.toThrow();
  });

  it("is never read from a build-time env var", async () => {
    const src = await import("node:fs").then((fs) => fs.readFileSync(new URL("../src/api.ts", import.meta.url), "utf8"));
    expect(src).not.toMatch(/import\.meta\.env/);
  });
});
