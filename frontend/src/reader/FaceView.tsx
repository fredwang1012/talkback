import { tryPlayQuietly } from "../audio/mediaErrors";
import { useEffect, useMemo, useRef } from "react";
import type { Face as FaceT } from "../api";
import { pulseAmount } from "./speechLevel";

/** A muted looping clip of a voice's face (speaking or idle). */
export function FaceLoop({ src }: { src: string }) {
  return <video key={src} src={src} muted loop autoPlay playsInline preload="auto" />;
}

/** The face for the medallion: a lip-synced clip, else the speaking loop while speaking, else the idle loop or portrait. */
export function FaceView({ face, speaking, clip }: { face: FaceT; speaking: boolean; clip: string | null }) {
  if (clip) return <video key={clip} src={clip} muted autoPlay playsInline />; // the audio element plays the sound
  if (speaking && face.talk) return <FaceLoop src={face.talk} />;
  if (face.idle) return <FaceLoop src={face.idle} />;
  return <img src={face.image} alt="" />;
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
    if (speaking) { void tryPlayQuietly(v); /* autoplay refused: the idle frame stays */ return; }
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
