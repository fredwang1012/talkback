/**
 * Access code for servers that set TALKBACK_ACCESS_TOKEN. It is typed in by the visitor and kept either for this
 * tab only (sessionStorage) or, when they choose "Remember on this device", in localStorage.
 * It must never be baked into the bundle (every VITE_* value is public), so there is deliberately no env fallback.
 */
const KEY = "talkback:access-code";

function read(get: () => Storage): string {
  try { return get().getItem(KEY) ?? ""; } catch { return ""; }
}

export function getAccessToken(): string {
  return read(() => sessionStorage) || read(() => localStorage);
}

/** True when the code is kept on this device (survives closing the tab). */
export function isAccessTokenRemembered(): boolean {
  return read(() => localStorage) !== "";
}

export function setAccessToken(code: string, remember = false): void {
  const put = (get: () => Storage, on: boolean) => {
    try { if (on) get().setItem(KEY, code); else get().removeItem(KEY); } catch { /* storage blocked: the code lasts until reload via the caller's state */ }
  };
  put(() => sessionStorage, !!code && !remember);
  put(() => localStorage, !!code && remember);
}

/** Forget the code everywhere on this device. */
export function clearAccessToken(): void {
  setAccessToken("");
}
