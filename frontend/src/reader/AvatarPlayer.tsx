import { useEffect, useRef, useState, type CSSProperties, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { Book } from "../api";
import type { TalkUi } from "../talk/useTalk";
import AskBar from "./AskBar";
import { isSwipeDown, latestExchange, playerKey, type PlayerMode, type Point } from "./playerLogic";
import { StageFace, type StageFaceProps } from "./StageFace";
import { speakerName } from "./talkbackLogic";

type Props = {
  mode: PlayerMode;
  onMode: (m: PlayerMode) => void;
  book: Book;
  name: string; // who the face is now (the reader of the line, or who answers)
  face: Omit<StageFaceProps, "name" | "className" | "talk">;
  talk: TalkUi;
  audioAvailable: boolean;
  playing: boolean;
  loading: boolean;
  onToggle: () => void;
  onPrev: () => void;
  onNext: () => void;
  atStart: boolean;
  atEnd: boolean;
  caption: string; // the sentence being read
  sinceId: number; // chat messages up to this id were there before this visit: not shown in the answer bubble
  onOpenTalkback?: () => void;
};

const Icon = ({ d }: { d: string }) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d={d} /></svg>
);
const PLAY = "M8 5v14l11-7z", PAUSE = "M6 5h4v14H6zM14 5h4v14h-4z", PREV = "M6 5h2v14H6zM20 5v14L9.5 12z", NEXT = "M16 5h2v14h-2zM4 5l10.5 7L4 19z";
const EXPAND = "M4 4h7v2H7.4l4.3 4.3-1.4 1.4L6 7.4V11H4zm16 16h-7v-2h3.6l-4.3-4.3 1.4-1.4 4.3 4.3V13h2z";
const COLLAPSE = "M11 11H4V9h3.6L3.3 4.7l1.4-1.4L9 7.6V4h2zm2 2h7v2h-3.6l4.3 4.3-1.4 1.4-4.3-4.3V20h-2z";
const CLOSE = "M6.4 5 5 6.4 10.6 12 5 17.6 6.4 19l5.6-5.6 5.6 5.6 1.4-1.4-5.6-5.6L19 6.4 17.6 5 12 10.6z";
const BUBBLE_HOLD_MS = 12000; // the answer bubble stays this long once reading goes on
const FOCUSABLE = "button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex]:not([tabindex='-1'])";

/**
 * The face as a podcast-style player (#33). Mini: a small corner card (face, name, play/pause, expand, hide). Expanded:
 * a near full-screen view (large face, the sentence being read, controls, the conversation and the ask bar); collapse
 * with its button, Esc or a swipe down. Hidden: only a small tab brings it back. The face is one element whatever the
 * mode (only its size changes), so the lip-sync videos and their syncs are never remounted; hidden unmounts it (the
 * caller then stops asking for clips). Answers asked while reading show in a dismissable bubble (#34).
 */
export default function AvatarPlayer(p: Props) {
  const { mode, onMode, talk } = p;
  const expanded = mode === "expanded";
  const root = useRef<HTMLDivElement>(null);
  const expandBtn = useRef<HTMLButtonElement>(null);
  const tab = useRef<HTMLButtonElement>(null);
  const moved = useRef(false); // the mode changed from a control here: move the focus with it
  const swipe = useRef<Point | null>(null);
  const log = useRef<HTMLDivElement>(null);
  const [bottom, setBottom] = useState(0);
  const [dismissed, setDismissed] = useState<number | null>(null);
  const exchange = latestExchange(talk.messages, p.sinceId);
  const idle = talk.state === "reading" && !talk.speaking;
  const bubble = !expanded && exchange && exchange.replyId !== dismissed && (exchange.answer || exchange.pending) ? exchange : null;

  const go = (m: PlayerMode) => { moved.current = true; onMode(m); };

  // focus follows the view: into the expanded view, back to the expand button, to the tab when hidden. The expanded
  // view itself takes the focus, not its first button: Hold Space must talk there (the Reader leaves Space on a button
  // to the button, which would collapse the view)
  useEffect(() => {
    if (!moved.current) return;
    moved.current = false;
    (mode === "expanded" ? root : mode === "hidden" ? tab : expandBtn).current?.focus();
  }, [mode]);

  // Esc collapses the expanded view (a dialog of its own handles Esc first)
  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.defaultPrevented) return;
      const next = playerKey("expanded", e.key);
      if (next) { e.preventDefault(); moved.current = true; onMode(next); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [expanded, onMode]);

  // sit above the player bar, whatever its height (it wraps on narrow screens)
  useEffect(() => {
    const bar = document.querySelector<HTMLElement>("footer.player");
    if (!bar || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setBottom(bar.offsetHeight));
    ro.observe(bar);
    return () => ro.disconnect();
  }, []);

  // the bubble goes away on its own a while after reading went on (keyed by the reply, not the bubble object: the
  // Reader re-renders on every word, which must not restart the wait)
  const doneReply = bubble && !bubble.pending ? bubble.replyId : null;
  useEffect(() => {
    if (doneReply === null || !idle) return;
    const t = window.setTimeout(() => setDismissed(doneReply), BUBBLE_HOLD_MS);
    return () => window.clearTimeout(t);
  }, [doneReply, idle]);

  useEffect(() => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [talk.messages, expanded]);

  /** Tab stays inside the expanded view (it covers the page). */
  const trap = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    if (!expanded || e.key !== "Tab" || !root.current) return;
    const items = [...root.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    const at = document.activeElement;
    if (e.shiftKey && (at === first || at === root.current)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && at === last) { e.preventDefault(); first.focus(); }
  };
  const swipeStart = (e: ReactPointerEvent<HTMLDivElement>) => {
    const interactive = (e.target as HTMLElement).closest?.("button, input, select, textarea, a, .ap-log");
    swipe.current = expanded && !interactive ? { x: e.clientX, y: e.clientY, t: e.timeStamp } : null;
  };
  const swipeEnd = (e: ReactPointerEvent<HTMLDivElement>) => {
    const from = swipe.current;
    swipe.current = null;
    if (from && isSwipeDown(from, { x: e.clientX, y: e.clientY, t: e.timeStamp })) go("mini");
  };

  const style = { "--ap-bottom": `${bottom}px` } as CSSProperties;
  const bubbleEl = bubble && (
    <div className="ap-bubble" role="status" aria-live="polite">
      <span className="who-line">{speakerName(p.book, bubble.speakerId)}</span>
      <p>{bubble.answer || "…"}</p>
      <button type="button" className="ap-bubble-close" aria-label="Dismiss the answer" onClick={() => setDismissed(bubble.replyId)}>
        <Icon d={CLOSE} />
      </button>
    </div>
  );

  if (mode === "hidden") {
    return (
      <div className="avatar-player-tab" style={style}>
        {bubbleEl}
        <button ref={tab} type="button" className="ap-tab" onClick={() => go("mini")} aria-label={`Show the face player (${p.name})`}>
          <span aria-hidden="true">{p.name.charAt(0)}</span> Show face
        </button>
      </div>
    );
  }

  const playLabel = p.playing ? "Pause reading" : p.audioAvailable ? "Play" : "Play silently";
  return (
    <>
      {!expanded && bubble && <div className="ap-bubble-wrap" style={style}>{bubbleEl}</div>}
      <div ref={root} className={`avatar-player ${mode}`} style={style}
           role={expanded ? "dialog" : "region"} aria-modal={expanded ? true : undefined} tabIndex={expanded ? -1 : undefined}
           aria-label={expanded ? `${p.name}, expanded player` : `Face player: ${p.name}`}
           onKeyDown={trap} onPointerDown={swipeStart} onPointerUp={swipeEnd} onPointerCancel={() => { swipe.current = null; }}>
        {expanded && (
          <div className="ap-top">
            <button type="button" className="icon-btn" onClick={() => go("mini")} aria-label="Collapse the player (Esc)">
              <Icon d={COLLAPSE} />
            </button>
          </div>
        )}
        <div className="ap-face">
          <StageFace {...p.face} name={p.name} talk={talk} />
        </div>
        <div className="ap-info">
          <h2 className="ap-name">{p.name}</h2>
          {expanded && <p className="ap-caption" aria-live="off">{p.caption ? `“${p.caption}”` : ""}</p>}
        </div>
        <div className="ap-controls">
          {expanded && (
            <button type="button" className="icon-btn" onClick={p.onPrev} disabled={p.atStart} aria-label="Previous sentence">
              <Icon d={PREV} />
            </button>
          )}
          <button type="button" className="icon-btn primary" onClick={p.onToggle} disabled={p.atEnd && !p.playing}
                  aria-label={playLabel} aria-busy={p.loading}>
            <Icon d={p.playing ? PAUSE : PLAY} />
          </button>
          {expanded && (
            <button type="button" className="icon-btn" onClick={p.onNext} disabled={p.atEnd} aria-label="Next sentence">
              <Icon d={NEXT} />
            </button>
          )}
          {!expanded && (
            <>
              <button ref={expandBtn} type="button" className="icon-btn" onClick={() => go("expanded")} aria-label="Expand the player">
                <Icon d={EXPAND} />
              </button>
              <button type="button" className="icon-btn" onClick={() => go("hidden")} aria-label="Hide the player">
                <Icon d={CLOSE} />
              </button>
            </>
          )}
        </div>
        {expanded && (
          <div className="ap-talk">
            <div className="ap-log" ref={log} role="log" aria-label="Conversation" tabIndex={0}>
              {talk.messages.length === 0 && <p className="empty-note">Nothing said yet. Ask {p.name} about the story so far.</p>}
              {talk.messages.slice(-30).map((m) => m.role === "tool" ? (
                <div key={m.id} className="tool-chip" role="status">{m.text}</div>
              ) : (
                <div key={m.id} className={`bubble ${m.role}${m.interrupted ? " interrupted" : ""}`}>
                  <span className="who-line">
                    {m.role === "user" ? "You" : m.role === "narrator" ? "Narrator" : speakerName(p.book, m.speakerId)}
                    {m.interrupted && <small> · interrupted</small>}
                  </span>
                  <p>{m.text || (m.pending || m.loading ? "…" : "")}</p>
                </div>
              ))}
            </div>
            <AskBar talk={talk} name={p.name} audioAvailable={p.audioAvailable} />
            {p.onOpenTalkback && <button type="button" className="link-btn" onClick={p.onOpenTalkback}>Open Talkback, the full conversation view</button>}
          </div>
        )}
      </div>
    </>
  );
}
