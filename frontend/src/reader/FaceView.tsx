import { useEffect, useMemo, useRef } from "react";
import type { Face as FaceT } from "../api";
import type { FaceSync } from "../talk/faceSync";
import { pulseAmount } from "./speechLevel";

/** A muted looping clip of a voice's face (speaking or idle). */
export function FaceLoop({ src }: { src: string }) {
  return <video key={src} src={src} muted loop autoPlay playsInline preload="auto" />;
}

/**
 * The face for the medallion (Talk mode). Layers, bottom to top, are never swapped for new elements, so nothing
 * flashes black: the idle loop (or portrait) always rests underneath; the talk loop fades in only while the sentence
 * audio is really playing and no lip-sync video is in step; the one persistent lip-sync <video> (driven by `sync`,
 * see talk/faceSync) fades in only once it has a decoded frame and is playing.
 */
export function FaceView({ face, speaking, sync, clipShown }: { face: FaceT; speaking: boolean; sync: FaceSync | null; clipShown: boolean }) {
  const talk = useRef<HTMLVideoElement>(null);
  const clip = useRef<HTMLVideoElement>(null);
  const showTalk = speaking && !clipShown && !!face.talk;

  useEffect(() => {
    const v = talk.current;
    if (!v) return;
    if (showTalk) { void v.play().catch(() => { /* autoplay refused: the idle frame stays */ }); return; }
    const t = window.setTimeout(() => v.pause(), 180); // after the cross-fade
    return () => window.clearTimeout(t);
  }, [showTalk]);

  useEffect(() => {
    sync?.setVideo(clip.current);
    return () => sync?.setVideo(null);
  }, [sync]);

  return (
    <div className="read-face">
      {face.idle
        ? <video key={face.idle} src={face.idle} muted loop autoPlay playsInline preload="auto" />
        : <img src={face.image} alt="" />}
      {face.talk && <video ref={talk} key={face.talk} src={face.talk} className={`rf-talk${showTalk ? " on" : ""}`} muted loop playsInline preload="auto" />}
      <video ref={clip} className={`rf-talk${clipShown ? " on" : ""}`} muted playsInline />
    </div>
  );
}

const reducedMotion = () => typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/**
 * The read-mode face. `speaking` follows the actual audio (see speechLevel): the talk loop shows only while there
 * is speech and the idle loop (or portrait) rests in pauses. With no talk loop, the picture pulses with the
 * loudness read from `levelRef`. Reduced motion shows the still portrait.
 */
export function ReadFace({ face, speaking, levelRef }: { face: FaceT; speaking: boolean; levelRef: { current: number } }) {
  const wrap = useRef<HTMLDivElement>(null);
  const talk = useRef<HTMLVideoElement>(null);
  const reduced = useMemo(reducedMotion, []);
  const hasTalk = !!face.talk && !reduced;

  useEffect(() => {
    const v = talk.current;
    if (!v) return;
    if (speaking) { void v.play().catch(() => { /* autoplay refused: the idle frame stays */ }); return; }
    const t = window.setTimeout(() => v.pause(), 180); // after the cross-fade
    return () => window.clearTimeout(t);
  }, [speaking, hasTalk]);

  useEffect(() => {
    const el = wrap.current;
    if (!el || hasTalk || reduced) return;
    let raf = 0, smooth = 0;
    const tick = () => {
      const target = speaking ? pulseAmount(levelRef.current) : 0;
      smooth += (target - smooth) * 0.35;
      el.style.transform = `scale(${(1 + smooth * 0.045).toFixed(4)})`;
      el.style.filter = `brightness(${(1 + smooth * 0.12).toFixed(3)})`;
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => { cancelAnimationFrame(raf); el.style.transform = ""; el.style.filter = ""; };
  }, [speaking, hasTalk, reduced, levelRef]);

  return (
    <div className="read-face" ref={wrap}>
      {face.idle && !reduced
        ? <video key={face.idle} src={face.idle} muted loop autoPlay playsInline preload="auto" />
        : <img src={face.image} alt="" />}
      {hasTalk && (
        <video ref={talk} key={face.talk!} src={face.talk!} className={`rf-talk${speaking ? " on" : ""}`} muted loop playsInline preload="auto" />
      )}
    </div>
  );
}
