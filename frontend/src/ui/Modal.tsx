import { useEffect, useRef, type ReactNode } from "react";

/** Centred modal on a native <dialog>, like the Add book dialog: showModal gives the inert background, focus
 * containment and Esc; a click on the backdrop closes it. The caller returns focus to its opener in onClose.
 * Children mount only while open, so each opening starts with a fresh form. */
export default function Modal({ open, onClose, labelledBy, children }: {
  open: boolean; onClose: () => void; labelledBy: string; children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} className="add-dialog" aria-labelledby={labelledBy} onClose={onClose}
      onCancel={(e) => { e.preventDefault(); onClose(); }}
      onMouseDown={(e) => { if (e.target === ref.current) onClose(); }}>
      {open && <div className="lab-dialog">{children}</div>}
    </dialog>
  );
}
