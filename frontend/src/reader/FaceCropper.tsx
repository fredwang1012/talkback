import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import { detectFace, type FaceBox } from "../api";
import { cropJpeg, moveCrop, proposeCrop, resizeCrop, type Crop } from "./faceCrop";

type Drag = { mode: "move" | "resize"; px: number; py: number; start: Crop; scale: number };

/** Proposes a head-and-shoulders square over the chosen photo (#109); the user drags or resizes it, then Use hands the
 * cropped portrait.jpg to onUse. Works without detection (a centred square near the top). */
export default function FaceCropper({ file, onUse, onCancel }: { file: File; onUse: (f: File) => void; onCancel: () => void }) {
  const url = useMemo(() => URL.createObjectURL(file), [file]);
  useEffect(() => () => URL.revokeObjectURL(url), [url]);
  const img = useRef<HTMLImageElement | null>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const [box, setBox] = useState<FaceBox | null | undefined>(undefined); // undefined: still finding
  const [crop, setCrop] = useState<Crop | null>(null);
  const [error, setError] = useState<string | null>(null);
  const touched = useRef(false);
  const drag = useRef<Drag | null>(null);

  useEffect(() => {
    const ctl = new AbortController();
    touched.current = false;
    setBox(undefined);
    setCrop(null);
    setError(null);
    void detectFace(file, ctl.signal).then((b) => { if (!ctl.signal.aborted) setBox(b); });
    return () => ctl.abort();
  }, [file]);
  useEffect(() => {
    if (size && !touched.current) setCrop(proposeCrop(box ?? null, size.w, size.h));
  }, [box, size]);

  function down(e: PointerEvent, mode: Drag["mode"]) {
    if (!crop || !size || !img.current) return;
    e.preventDefault();
    e.stopPropagation();
    e.currentTarget.setPointerCapture?.(e.pointerId);
    touched.current = true;
    drag.current = { mode, px: e.clientX, py: e.clientY, start: crop, scale: size.w / img.current.getBoundingClientRect().width };
  }
  function move(e: PointerEvent) {
    const d = drag.current;
    if (!d || !size) return;
    const dx = (e.clientX - d.px) * d.scale, dy = (e.clientY - d.py) * d.scale;
    setCrop((d.mode === "move" ? moveCrop : resizeCrop)(d.start, dx, dy, size.w, size.h));
  }
  function key(e: KeyboardEvent) {
    if (!crop || !size) return;
    const step = Math.max(4, Math.round(crop.side * 0.05));
    const moves: Record<string, [number, number]> = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] };
    if (moves[e.key]) setCrop(moveCrop(crop, ...moves[e.key], size.w, size.h));
    else if (e.key === "+" || e.key === "=") setCrop(resizeCrop(crop, step, step, size.w, size.h));
    else if (e.key === "-") setCrop(resizeCrop(crop, -step, -step, size.w, size.h));
    else return;
    e.preventDefault();
    touched.current = true;
  }
  async function use() {
    if (!crop || !img.current) return;
    try {
      onUse(await cropJpeg(img.current, crop));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  const note = box === undefined ? "Finding your face…"
    : box ? "Drag the square to adjust, or pull its corner to resize." : "No face found: place the square over your head and shoulders.";
  return (
    <span className="crop">
      <span className="crop-frame" onPointerMove={move} onPointerUp={() => { drag.current = null; }} onPointerCancel={() => { drag.current = null; }}>
        <img ref={img} src={url} alt="Chosen photo" draggable={false}
             onLoad={(e) => setSize({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })} />
        {crop && size && (
          <span className="crop-square" role="slider" tabIndex={0} aria-label="Portrait crop (arrow keys move, + and - resize)"
                aria-valuetext={`${crop.side} px square`} onPointerDown={(e) => down(e, "move")} onKeyDown={key}
                style={{ left: pct(crop.x, size.w), top: pct(crop.y, size.h), width: pct(crop.side, size.w), height: pct(crop.side, size.h) }}>
            <span className="crop-handle" aria-hidden="true" onPointerDown={(e) => down(e, "resize")} />
          </span>
        )}
      </span>
      <span role="status" className="empty-note">{note}</span>
      {error && <span role="alert" className="lab-error">{error}</span>}
      <span className="camera-box">
        <button type="button" className="primary" disabled={!crop} onClick={() => void use()}>Use</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </span>
    </span>
  );
}
