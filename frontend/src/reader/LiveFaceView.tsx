import { useEffect, useRef } from "react";
import type { LiveMedia } from "../talk/liveFace";

/** The live face's video, with its own audio (#140): voice and lips come from one stream, so they stay in sync. */
export function LiveFaceView({ media, name }: { media: LiveMedia; name: string }) {
  const video = useRef<HTMLVideoElement | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  useEffect(() => {
    if (video.current) video.current.srcObject = media.video ? new MediaStream([media.video]) : null;
  }, [media.video]);
  useEffect(() => {
    if (!audio.current) return;
    audio.current.srcObject = media.audio ? new MediaStream([media.audio]) : null;
    if (media.audio) void audio.current.play().catch(() => {});
  }, [media.audio]);
  return (
    <div className="live-face">
      <video ref={video} autoPlay playsInline muted aria-label={`${name}, live`} />
      <audio ref={audio} autoPlay />
      {!media.video && <span className="live-face-wait">Connecting…</span>}
    </div>
  );
}
