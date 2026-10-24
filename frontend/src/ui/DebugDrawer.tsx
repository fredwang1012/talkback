// Adapted from boson-ai/higgs-realtime-tutorial (Apache-2.0). Modified for Talkback.
import { useEffect, useState } from "react";
import type { SpendSnapshot } from "../spend";

export interface LogEntry {
  id: number;
  dir: "recv" | "send" | "info";
  type: string;
  at: number;
  raw: unknown;
}

/**
 * Every Realtime event, in order, verbatim, plus the running Boson spend estimate.
 * The Realtime API is a stream of events you did not write and cannot step through in a debugger; this is how you
 * find out what the server actually said. Toggle with the backtick key.
 */
export function DebugDrawer({ entries, spend }: { entries: LogEntry[]; spend: SpendSnapshot }) {
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "`" && !["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName)) setOpen((o) => !o);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <section className={`drawer${open ? " open" : ""}`} aria-label="Debug">
      <button className="drawer-toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        Debug · about ${spend.usd.toFixed(3)} spent this session
      </button>
      {open && (
        <>
          <p className="drawer-spend">
            Speech: {spend.ttsChars} characters · Voice in: {spend.inSeconds.toFixed(0)} s · Voice out: {spend.outSeconds.toFixed(0)} s.
            Estimate from published rates; set a monthly cap in the Boson workspace.
          </p>
          <ol className="drawer-list">
            {entries.length === 0 && <li className="entry"><span className="time">No events yet.</span></li>}
            {entries.map((e) => (
              <li key={e.id} className={`entry dir-${e.dir}`}>
                <button className="entry-head" onClick={() => setExpanded(expanded === e.id ? null : e.id)}>
                  <span className="arrow">{e.dir === "send" ? "↑" : e.dir === "recv" ? "↓" : "·"}</span>
                  <code>{e.type}</code>
                  <span className="time">{new Date(e.at).toLocaleTimeString(undefined, { hour12: false })}</span>
                </button>
                {expanded === e.id && <pre className="entry-body">{formatPayload(e.raw)}</pre>}
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}

/** Audio deltas are ~1300 characters of base64 each; shorten them so they do not bury everything else. */
function formatPayload(raw: unknown): string {
  if (typeof raw === "string") return raw;
  const replacer = (_key: string, value: unknown) =>
    typeof value === "string" && value.length > 120 ? `${value.slice(0, 60)}… (${value.length} chars)` : value;
  return JSON.stringify(raw, replacer, 2);
}
