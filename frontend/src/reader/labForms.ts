/** Pure helpers for the Voice lab's Add voice / Add face dialogs (#111). Free of React so they can be unit-tested. */

export type VoiceForm = {
  clip: boolean;        // a recording or an uploaded file is ready
  transcript: string;
  name: string;
  consent: boolean;
  audioAvailable: boolean;
  busy: boolean;
};

/** What still stops "Create voice", in plain words; empty when it can be pressed. */
export function voiceMissing(f: VoiceForm): string[] {
  if (!f.audioAvailable) return ["voice cloning on the server (it needs the Boson API key and, if asked, your access code)"];
  const out: string[] = [];
  if (!f.clip) out.push("a recording or an audio file");
  if (!f.transcript.trim()) out.push("the words you said");
  if (!f.name.trim()) out.push("a name for the voice");
  if (!f.consent) out.push("the consent box ticked");
  return out;
}

export type FaceForm = {
  file: boolean;      // a cropped portrait is ready
  cropping: boolean;  // a photo was chosen but its crop has not been confirmed with Use
  name: string;
  busy: boolean;
};

/** What still stops "Upload", in plain words; empty when it can be pressed. */
export function faceMissing(f: FaceForm): string[] {
  const out: string[] = [];
  if (!f.name.trim()) out.push("a name for the face");
  if (f.cropping) out.push("Use under the photo to keep the crop");
  else if (!f.file) out.push("a photo (choose a file or take one)");
  return out;
}

/** "Still needed: a, b and c." or null when nothing is missing. */
export function missingLine(items: string[]): string | null {
  if (items.length === 0) return null;
  const list = items.length === 1 ? items[0] : `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
  return `Still needed: ${list}.`;
}

type UrlApi = { createObjectURL(b: Blob): string; revokeObjectURL(u: string): void };

/**
 * Effect body for a file's preview URL: creates it, hands it to setUrl, and revokes it on cleanup. Creating the URL in
 * the effect (not in useMemo) matters under StrictMode, which runs mount effects twice: a memoised URL was revoked by
 * the first cleanup and never recreated, so the first photo chosen never loaded (#111).
 */
export function objectUrlEffect(file: Blob, setUrl: (u: string | null) => void, api: UrlApi = URL): () => void {
  const u = api.createObjectURL(file);
  setUrl(u);
  return () => { api.revokeObjectURL(u); setUrl(null); };
}

/** The natural size of an image that has finished loading, else null (covers a load that fired before React saw it). */
export function loadedSize(img: { complete: boolean; naturalWidth: number; naturalHeight: number } | null): { w: number; h: number } | null {
  if (!img || !img.complete || img.naturalWidth <= 0 || img.naturalHeight <= 0) return null;
  return { w: img.naturalWidth, h: img.naturalHeight };
}
