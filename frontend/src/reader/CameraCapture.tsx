import { useEffect, useRef, useState } from "react";
import { cameraErrorMessage, captureJpeg, stopStream } from "../audio/camera";

/** "Take photo" button with an inline webcam preview. Hands the captured portrait.jpg to onUse like a file input would. */
export default function CameraCapture({ onUse }: { onUse: (file: File) => void }) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [shot, setShot] = useState<{ file: File; url: string } | null>(null);
  const video = useRef<HTMLVideoElement | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const alive = useRef(true);

  function stop() {
    stopStream(stream.current);
    stream.current = null;
  }
  useEffect(() => {
    alive.current = true;
    return () => { alive.current = false; stop(); };
  }, []);
  useEffect(() => () => { if (shot) URL.revokeObjectURL(shot.url); }, [shot]);

  async function start() {
    stop();
    setError(null);
    setShot(null);
    setOpen(true);
    try {
      const s = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user" } });
      if (!alive.current) { stopStream(s); return; }
      stream.current = s;
      if (video.current) { video.current.srcObject = s; void video.current.play?.()?.catch(() => {}); }
    } catch (e) {
      setOpen(false);
      setError(cameraErrorMessage(e));
    }
  }
  function cancel() { stop(); setOpen(false); setShot(null); }
  async function capture() {
    if (!video.current) return;
    try {
      const file = await captureJpeg(video.current);
      stop();
      setShot({ file, url: URL.createObjectURL(file) });
    } catch (e) {
      stop();
      setOpen(false);
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <span className="camera">
      {!open && <button type="button" onClick={() => void start()}>Take photo</button>}
      {error && <p role="alert" className="lab-error">{error}</p>}
      {open && !shot && (
        <span className="camera-box">
          <video ref={video} className="camera-preview" muted playsInline aria-label="Camera preview" />
          <button type="button" className="primary" onClick={() => void capture()}>Capture</button>
          <button type="button" onClick={cancel}>Cancel</button>
        </span>
      )}
      {open && shot && (
        <span className="camera-box">
          <img className="camera-thumb" src={shot.url} alt="Captured photo" />
          <button type="button" className="primary" onClick={() => { onUse(shot.file); cancel(); }}>Use photo</button>
          <button type="button" onClick={() => void start()}>Retake</button>
        </span>
      )}
    </span>
  );
}
