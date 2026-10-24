import { useEffect, useId, useRef, useState } from "react";
import type { RecapScope } from "../api";

/** `section`: a chapter asked for by name ("summarize chapter 4") instead of the last / current one. */
export type RecapChoice = { scope: RecapScope; label: string; section?: string };

/** The menu's options: the series one only for a book that belongs to a series. A textbook recaps the chapter the
 * reader is in (no spoilers), so its first option is "This chapter". */
export function recapChoices(inSeries: boolean, textbook = false): RecapChoice[] {
  const all: RecapChoice[] = [
    { scope: "chapter", label: textbook ? "This chapter" : "Last chapter" },
    { scope: "book", label: "This book so far" },
    { scope: "series", label: "The series so far" },
  ];
  return inSeries ? all : all.filter((c) => c.scope !== "series");
}

/** Where focus goes for a key inside an open menu of `count` items (null: the key is not for the menu). */
export function menuKey(index: number, count: number, key: string): number | null {
  if (key === "ArrowDown") return (index + 1) % count;
  if (key === "ArrowUp") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

type Props = { inSeries: boolean; textbook?: boolean; onPick: (c: RecapChoice) => void; className?: string; disabled?: boolean };

/** "Recap" menu button (WAI-ARIA menu button): Enter/Space/ArrowDown open it on the first item, ArrowUp on the last;
 * arrows, Home and End move; Esc closes and returns focus to the button; Tab or a click outside closes it. */
export default function RecapMenu({ inSeries, textbook = false, onPick, className = "link-btn", disabled }: Props) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const button = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const wrap = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const choices = recapChoices(inSeries, textbook);

  useEffect(() => { if (open) items.current[active]?.focus(); }, [open, active]);
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [open]);

  const show = (at: number) => { setActive(at); setOpen(true); };
  const close = (refocus: boolean) => { setOpen(false); if (refocus) button.current?.focus(); };
  const pick = (c: RecapChoice) => { close(true); onPick(c); };

  return (
    <div className="recap-menu" ref={wrap}>
      <button ref={button} type="button" className={className} disabled={disabled}
              aria-haspopup="menu" aria-expanded={open} aria-controls={open ? menuId : undefined}
              onClick={() => (open ? close(false) : show(0))}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") { e.preventDefault(); show(0); }
                else if (e.key === "ArrowUp") { e.preventDefault(); show(choices.length - 1); }
              }}>
        Recap
      </button>
      {open && (
        <div id={menuId} role="menu" aria-label="Recap" className="recap-menu-list"
             onKeyDown={(e) => {
               if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(true); return; }
               if (e.key === "Tab") { close(false); return; }
               const next = menuKey(active, choices.length, e.key);
               if (next !== null) { e.preventDefault(); setActive(next); }
             }}>
          {choices.map((c, i) => (
            <button key={c.scope} type="button" role="menuitem" tabIndex={i === active ? 0 : -1}
                    ref={(el) => { items.current[i] = el; }} onClick={() => pick(c)}>
              {c.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
