import { useId, useRef, useState } from "react";
import type { TalkState } from "../talk/TalkController";
import type { TalkUi } from "../talk/useTalk";

const STATUS: Record<TalkState, string> = {
  reading: "Hold Space or the mic to ask, or type below. Start with a name to ask someone else.",
  listening: "Listening…",
  transcribing: "Hearing what you said…",
  retrieving: "Checking the story so far…",
  answering: "Answering… Speak or press the mic to cut in.",
  resuming: "Picking the story back up…",
  "talk-idle": "Ask another question, or press play.",
};

type Props = {
  talk: TalkUi;
  name: string; // who answers unless the question names someone else
  audioAvailable: boolean;
  unavailableNote?: string; // why the mic is off (no Boson key)
};

/**
 * Talk while reading (#34): hold-to-talk mic, hands-free switch and a type box on the reading screen, on the same
 * talk loop as Talkback (useTalk / TalkController). A question pauses the reading, the character answers and reading
 * picks up at the same sentence (auto-resume); speaking or pressing the mic over an answer cuts it off.
 */
export default function AskBar({ talk, name, audioAvailable, unavailableNote }: Props) {
  const id = useId();
  const [draft, setDraft] = useState("");
  const held = useRef(false);
  const press = () => { if (!held.current) { held.current = true; talk.pttDown(); } };
  const release = () => { if (held.current) { held.current = false; talk.pttUp(); } };
  const listening = talk.state === "listening";
  const busy = ["transcribing", "retrieving", "answering"].includes(talk.state) || talk.speaking;
  const send = () => {
    const t = draft.trim();
    if (!t) return;
    setDraft("");
    talk.ask(t);
  };

  return (
    <div className="ask-bar">
      <div className="mic">
        <button
          type="button"
          className={`mic-button${listening ? " live" : ""}`}
          disabled={!audioAvailable}
          aria-label={audioAvailable ? `Hold to ask ${name}` : "Hold to talk (needs the Boson API key)"}
          aria-pressed={listening}
          onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); press(); }}
          onPointerUp={release}
          onPointerCancel={release}
          onBlur={release}
          onKeyDown={(e) => { if ((e.code === "Space" || e.code === "Enter") && !e.repeat) { e.preventDefault(); press(); } }}
          onKeyUp={(e) => { if (e.code === "Space" || e.code === "Enter") { e.preventDefault(); release(); } }}
        >
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
            <rect x="9" y="3" width="6" height="12" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
          </svg>
        </button>
        <div>
          <p aria-live="polite">{audioAvailable ? STATUS[talk.state] : unavailableNote ?? "Voice questions need the Boson API key on the server."}</p>
          {listening && <div className="meter" aria-hidden="true"><span style={{ width: `${Math.min(100, talk.level * 600)}%` }} /></div>}
        </div>
      </div>
      {audioAvailable && (
        <form className="ask-text" onSubmit={(e) => { e.preventDefault(); send(); }}>
          <label className="sr-only" htmlFor={`${id}-q`}>Type a question for {name}</label>
          <input id={`${id}-q`} type="text" value={draft} maxLength={500} autoComplete="off" placeholder={`Ask ${name}…`}
                 onChange={(e) => setDraft(e.target.value)} />
          <button type="submit" disabled={!draft.trim()}>Ask</button>
          {busy && <button type="button" onClick={talk.stop}>Stop</button>}
        </form>
      )}
      {audioAvailable && talk.setHandsFree && (
        <div className="ask-hands-free">
          <button type="button" className="spoiler-toggle hands-free" aria-pressed={!!talk.handsFree}
                  aria-describedby={`${id}-hf`} onClick={() => talk.setHandsFree?.(!talk.handsFree)}>
            Hands-free: {talk.handsFree ? "on" : "off"}
          </button>
          <small id={`${id}-hf`} className="headphone-hint">
            {talk.handsFree
              ? "The mic stays open: just speak to ask or to cut in, a pause ends your question. Headphones keep the book's voice out of the mic."
              : "Use headphones for hands-free: through speakers the mic also hears the book being read."}
          </small>
        </div>
      )}
    </div>
  );
}
