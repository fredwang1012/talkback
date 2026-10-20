import type { Face as FaceT } from "../api";

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
