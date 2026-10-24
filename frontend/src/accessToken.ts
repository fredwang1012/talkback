/**
 * Access code for servers that set TALKBACK_ACCESS_TOKEN. It is typed in by the visitor and kept for this tab only.
 * It must never be baked into the bundle (every VITE_* value is public), so there is deliberately no env fallback.
 */
const KEY = "talkback:access-code";

export function getAccessToken(): string {
  try { return sessionStorage.getItem(KEY) ?? ""; } catch { return ""; }
}

export function setAccessToken(code: string): void {
  try {
    if (code) sessionStorage.setItem(KEY, code);
    else sessionStorage.removeItem(KEY);
  } catch { /* storage blocked: the code lasts until reload via the caller's state */ }
}
