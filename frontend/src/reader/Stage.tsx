import { useRef, useState } from "react";
import type { Book, Health } from "../api";
import type { TalkUi } from "../talk/useTalk";
import type { TalkState } from "../talk/TalkController";
import { splitCharacters } from "./position";

type Props = {
  book: Book;
  para: number;
  speakerId: string;
  onSpeaker: (id: string) => void;
  health: Health | null;
  talk: TalkUi;
  audioAvailable: boolean;
  onTalkMode: () => void;
  pinned: string | null; // demo mode: talk only to this character
  needsCode: boolean;
  onCode: (code: string) => void;
  onOpenLab: () => void;
};

const STATUS: Record<TalkState, string> = {
  reading: "Hold Space or the button to ask a question.",
  listening: "Listening…",
  transcribing: "Hearing what you said…",
  retrieving: "Checking the story so far…",
  answering: "Answering…",
  resuming: "Picking the story back up…",
  "talk-idle": "Ask another question, or press play.",
};

export default function Stage({ book, para, speakerId, onSpeaker, health, talk, audioAvailable, onTalkMode, pinned, needsCode, onCode, onOpenLab }: Props) {
  const [codeDraft, setCodeDraft] = useState("");
  const [mode, setMode] = useState<"read" | "talk">("read");
  const { met, locked } = splitCharacters(book.characters, para);
  const speaker = met.find((c) => c.id === speakerId);
  const name = speaker?.name ?? "Narrator";
  const blurb = speaker ? speaker.persona : book.narrator?.is_character
    ? `Tells the story as ${book.characters.find((c) => c.id === book.narrator?.is_character)?.name ?? "a character"}.`
    : "Reads the text as written.";
  const live = talk.state === "listening";
  const pressed = useRef(false); // survives re-renders between press and release

  const press = () => { if (!pressed.current) { pressed.current = true; talk.pttDown(); } };
  const release = () => { if (pressed.current) { pressed.current = false; talk.pttUp(); } };

  return (
    <aside className="stage" aria-label="Stage">
      <div className="mode" role="group" aria-label="Mode">
        <button aria-pressed={mode === "read"} onClick={() => setMode("read")}>Read</button>
        <button
          aria-pressed={mode === "talk"}
          disabled={!audioAvailable}
          title={audioAvailable ? "" : "Talk needs the Boson API key on the server"}
          onClick={() => { setMode("talk"); onTalkMode(); }}
        >
          Talk
        </button>
      </div>

      <div className="speaker">
        <div className={`medallion${live ? " live" : ""}`} aria-hidden="true">{name.charAt(0)}</div>
        <div>
          <h3>{name}</h3>
          <p>{blurb}</p>
        </div>
      </div>

      <div className="badge">
        <strong>{speaker ? `${name} knows` : "Story position:"}</strong> up to paragraph {para + 1}.
        {locked.length > 0 && <> {locked.length} more {locked.length === 1 ? "character appears" : "characters appear"} later.</>}
      </div>

      <section aria-label="Who to talk to">
        <ul className="cast-list">
          <li>
            <button aria-pressed={speakerId === "narrator"} disabled={!!pinned} onClick={() => onSpeaker("narrator")}>
              Narrator <small>{book.narrator?.voice ?? "voice not set"}</small>
            </button>
          </li>
          {met.map((c) => (
            <li key={c.id}>
              <button aria-pressed={speakerId === c.id} disabled={!!pinned && pinned !== c.id} onClick={() => onSpeaker(c.id)}>
                {c.name} <small>{c.voice}</small>
              </button>
            </li>
          ))}
          {locked.map((c) => (
            <li key={c.id}>
              <button disabled title="Not met yet">
                {c.name} <small>not met yet</small>
              </button>
            </li>
          ))}
        </ul>
        {book.characters.length === 0 && (
          <p className="empty-note">No cast yet. Characters appear here once the casting step has run.</p>
        )}
      </section>

      {needsCode && (
        <form className="code-form" onSubmit={(e) => { e.preventDefault(); if (codeDraft.trim()) onCode(codeDraft.trim()); }}>
          <label htmlFor="access-code">This server needs an access code for voices</label>
          <div>
            <input id="access-code" type="password" autoComplete="off" value={codeDraft} onChange={(e) => setCodeDraft(e.target.value)} />
            <button type="submit">Unlock voices</button>
          </div>
        </form>
      )}
      <button className="link-btn" onClick={onOpenLab}>Voices and cloning</button>

      <div className="mic">
        <button
          className={`mic-button${live ? " live" : ""}`}
          disabled={!audioAvailable}
          aria-label={audioAvailable ? "Hold to talk" : "Hold to talk (needs the Boson API key)"}
          onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); press(); }}
          onPointerUp={release}
          onPointerCancel={release}
        >
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
            <rect x="9" y="3" width="6" height="12" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
          </svg>
        </button>
        <div>
          <p aria-live="polite">
            {audioAvailable ? STATUS[talk.state] : health?.boson_key_set === false || !health
              ? "Voice questions need the Boson API key on the server."
              : STATUS[talk.state]}
          </p>
          {live && <div className="meter" aria-hidden="true"><span style={{ width: `${Math.min(100, talk.level * 600)}%` }} /></div>}
        </div>
      </div>

      <div className="captions" aria-live="polite">
        {talk.caption || (audioAvailable ? "" : "Captions of the answer will appear here.")}
      </div>
      {talk.latency?.firstAudioMs !== undefined && (
        <p className="empty-note">
          Last answer: heard in {talk.latency.commitToTranscriptMs ?? "?"} ms, looked up in {talk.latency.retrieveMs ?? "?"} ms,
          first sound after {talk.latency.firstAudioMs} ms.
        </p>
      )}
      {talk.toast && (
        <div className="toast" role="alert">
          <span>{talk.toast}</span>
          <button onClick={talk.dismissToast} aria-label="Dismiss">Close</button>
        </div>
      )}
    </aside>
  );
}
