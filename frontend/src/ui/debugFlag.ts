/** The debug drawer (and its spend pill) shows only with `?debug` in the URL or localStorage `talkback:debug=1`. */
export function debugEnabled(
  search: string = typeof location === "undefined" ? "" : location.search,
  storage: Pick<Storage, "getItem"> | undefined = (() => { try { return localStorage; } catch { return undefined; } })(),
): boolean {
  if (new URLSearchParams(search).has("debug")) return true;
  try { return storage?.getItem("talkback:debug") === "1"; } catch { return false; }
}
