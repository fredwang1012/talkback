/**
 * Live face state for the reader (#140): which preset the reader picked (remembered per browser), and the running
 * session while the Talkback view is open. Leaving Talkback, picking Off, or closing the page releases the session,
 * because Boson bills the slot while it is held. When no slot is free the pick stays but answers keep the usual
 * per-sentence clips, with a note saying so.
 */
import { useEffect, useState } from "react";
import { LiveUnavailable, fetchLiveAvatars, type LiveAvatarInfo } from "../api";
import { LiveFace, type LiveMedia } from "./liveFace";

const KEY = "talkback:liveFace";

function readPick(): string {
  try { return localStorage.getItem(KEY) ?? ""; } catch { return ""; }
}

export type LiveFaceUi = {
  info: LiveAvatarInfo; // enabled false: the picker is hidden
  pick: string; // "" = off
  setPick: (id: string) => void;
  face: LiveFace | null; // running and joined
  media: LiveMedia;
  status: "off" | "starting" | "live" | "unavailable" | "error";
  note: string | null;
};

export function useLiveFace(active: boolean): LiveFaceUi {
  const [info, setInfo] = useState<LiveAvatarInfo>({ enabled: false, avatars: [] });
  const [pick, setPickState] = useState(readPick);
  const [face, setFace] = useState<LiveFace | null>(null);
  const [media, setMedia] = useState<LiveMedia>({ video: null, audio: null });
  const [status, setStatus] = useState<LiveFaceUi["status"]>("off");
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    fetchLiveAvatars().then((i) => { if (live) setInfo(i); }).catch(() => {});
    return () => { live = false; };
  }, []);

  const setPick = (id: string) => {
    setPickState(id);
    try { localStorage.setItem(KEY, id); } catch { /* blocked */ }
  };

  const wanted = active && info.enabled && !!pick && info.avatars.some((a) => a.id === pick);
  useEffect(() => {
    if (!wanted) { setStatus("off"); setNote(null); return; }
    let cancelled = false;
    let mine: LiveFace | null = null;
    setStatus("starting");
    setNote(null);
    LiveFace.start(pick).then((f) => {
      if (cancelled) { void f.close(); return; }
      mine = f;
      f.onMedia = (m) => setMedia(m);
      f.onEnded = () => { setFace(null); setMedia({ video: null, audio: null }); setStatus("off"); setNote("The live face ended; using the usual face clips."); };
      setMedia(f.mediaNow);
      setFace(f);
      setStatus("live");
    }).catch((e: unknown) => {
      if (cancelled) return;
      setStatus(e instanceof LiveUnavailable ? "unavailable" : "error");
      setNote(e instanceof Error ? e.message : String(e));
    });
    const leave = () => { void mine?.close(); };
    window.addEventListener("pagehide", leave);
    return () => {
      cancelled = true;
      window.removeEventListener("pagehide", leave);
      void mine?.close();
      setFace(null);
      setMedia({ video: null, audio: null });
    };
  }, [wanted, pick]);

  return { info, pick, setPick, face, media, status, note };
}
