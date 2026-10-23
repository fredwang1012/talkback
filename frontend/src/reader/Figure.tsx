import { useEffect, useRef, useState, type ReactNode } from "react";
import { fetchFigure } from "../api";

type Props = { bookId: string; name: string; caption: string; para: number; children: ReactNode };

/**
 * A textbook figure (#40) between paragraphs: its image, fetched only once it is near the screen (with the access
 * code, so it arrives as an object URL), and its caption underneath, which is read aloud like a sentence (`children`).
 * Tapping the image shows it larger; Escape or a click closes that view.
 */
export default function Figure({ bookId, name, caption, para, children }: Props) {
  const box = useRef<HTMLElement>(null);
  const [near, setNear] = useState(false);
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const el = box.current;
    if (!el || typeof IntersectionObserver === "undefined") { setNear(true); return; }
    const io = new IntersectionObserver((es) => { if (es.some((e) => e.isIntersecting)) { setNear(true); io.disconnect(); } },
      { rootMargin: "600px 0px" });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  useEffect(() => {
    if (!near) return;
    let url: string | null = null;
    let live = true;
    setFailed(false);
    fetchFigure(bookId, name).then((u) => { url = u; if (live) setSrc(u); else URL.revokeObjectURL(u); }, () => { if (live) setFailed(true); });
    return () => { live = false; if (url) URL.revokeObjectURL(url); setSrc(null); };
  }, [near, bookId, name]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <figure className="para para-figure" data-para={para} ref={box}>
      {src ? (
        <button className="figure-img" onClick={() => setOpen(true)} aria-label={`Enlarge: ${caption}`}>
          <img src={src} alt={caption} />
        </button>
      ) : (
        <div className="figure-img figure-wait" role="img" aria-label={caption}>{failed ? "This figure could not be loaded." : null}</div>
      )}
      <figcaption>{children}</figcaption>
      {open && src && (
        <div className="figure-zoom" role="dialog" aria-modal="true" aria-label={caption} onClick={() => setOpen(false)}>
          <img src={src} alt={caption} />
          <p>{caption}</p>
        </div>
      )}
    </figure>
  );
}
