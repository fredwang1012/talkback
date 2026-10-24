/** Per-book "full book" (spoilers on) choice. Off by default; storage failures just mean "off". */
const key = (bookId: string) => `talkback:spoilers:${bookId}`;

export function loadSpoilers(bookId: string): boolean {
  try { return localStorage.getItem(key(bookId)) === "1"; } catch { return false; }
}

export function saveSpoilers(bookId: string, on: boolean): void {
  try {
    if (on) localStorage.setItem(key(bookId), "1"); else localStorage.removeItem(key(bookId));
  } catch { /* storage blocked */ }
}
