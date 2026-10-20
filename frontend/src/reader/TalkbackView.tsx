import { useEffect, useRef, useState } from "react";
import type { Book, Face as FaceT } from "../api";
import type { TalkUi } from "../talk/useTalk";
import { PRESETS, type TalkChoice } from "./castLogic";
import { FaceView } from "./FaceView";
import { chapterAt, showSpoilerToggle, speakerChoices, speakerName, talkbackStatus, type TalkbackStatus } from "./talkbackLogic";

type Props = {
  book: Book;
  para: number;
  speakerId: string;
  onSpeaker: (id: string) => void;
  pinned: string | null; // demo mode: only this character
  talk: TalkUi;
  faces: FaceT[];
  faceFor: (speakerId: string) => FaceT | undefined;
  autoVoiceFor: (speakerId: string) => string;
  choice: TalkChoice;
  onChoice: (c: TalkChoice) => void;
  voices: { value: string; label: string }[];
  spoilers: boolean;
  onSpoilers: (on: boolean) => void;
  lipSyncable: boolean;
  lipSync: boolean;
  onLipSync: (on: boolean) => void;
  portrait: string | null;
  onBack: () => void;
};

const STATUS_TEXT: Record<TalkbackStatus, string> = {
  idle: "Ready. Hold the microphone (or Space) to ask, or type below.",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking…",
};

/** Read | Talkback segmented toggle, shown at the top of the reader. */
export function ModeToggle({ mode, onMode, talkAvailable }: { mode: "read" | "talk"; onMode: (m: "read" | "talk") => void; talkAvailable: boolean }) {
  return (
    <div className="mode mode-top" role="group" aria-label="Mode">
      <button aria-pressed={mode === "read"} onClick={() => onMode("read")}>Read</button>
      <button aria-pressed={mode === "talk"} disabled={!talkAvailable} onClick={() => onMode("talk")}
              title={talkAvailable ? "Press T to switch" : "Talkback needs the Boson API key on the server"}>
        Talkback
      </button>
    </div>
  );
}

export default function TalkbackView({
  book, para, speakerId, onSpeaker, pinned, talk, faces, faceFor, autoVoiceFor, choice, onChoice, voices,
  spoilers, onSpoilers, lipSyncable, lipSync, onLipSync, portrait, onBack,
}: Props) {
  const novel = showSpoilerToggle(book);
  const fullBook = novel && spoilers;
  const choices = speakerChoices(book, para, fullBook);
  const name = speakerName(book, speakerId);
  const status = talkbackStatus(talk.state, talk.speaking);
  const face = faceFor(speakerId);
  const chapter = chapterAt(book, para);
  const voiceLabel = (v: string) => (PRESETS.includes(v) ? v : "your cloned voice");
  const [draft, setDraft] = useState("");
  const log = useRef<HTMLDivElement>(null);
  const held = useRef(false);

  useEffect(() => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight; // instant: no motion to reduce
  }, [talk.messages]);

  const press = () => { if (!held.current) { held.current = true; talk.pttDown(); } };
  const release = () => { if (held.current) { held.current = false; talk.pttUp(); } };
  const busy = status === "thinking" || status === "speaking";

  const send = () => {
    const t = draft.trim();
    if (!t) return;
    setDraft("");
    talk.ask(t);
  };

  return (
    <section className="talkback" aria-label="Talkback conversation">
      <div className="tb-top">
        <button className="link-btn" onClick={onBack}>Back to reading</button>
        <div className={`badge${fullBook ? " spoilers-on" : ""}`}>
          <strong>You're at {chapter ?? `paragraph ${para + 1}`}.</strong>
          {fullBook && <> Spoilers on — whole book.</>}
        </div>
      </div>

      <div className="tb-main">
        <div className="tb-face-col">
          <div className={`tb-face${status === "listening" ? " live" : ""}${status === "thinking" ? " thinking" : ""}${status === "speaking" ? " speaking" : ""}`} aria-hidden="true">
            {face ? <FaceView face={face} speaking={talk.speaking} clip={talk.clip} />
              : portrait ? <img src={portrait} alt="" />
              : <span className="mono">{name.charAt(0)}</span>}
          </div>
          <h2 className="tb-name">{name}</h2>

          <div className="tb-controls">
            {choices.length > 0 && (
              <>
                <label htmlFor="tb-who">Talk to</label>
                <select id="tb-who" value={speakerId} disabled={!!pinned} onChange={(e) => onSpeaker(e.target.value)}>
                  {choices.map((c) => <option key={c.id} value={c.id} disabled={!!pinned && pinned !== c.id}>{c.name}</option>)}
                </select>
              </>
            )}
            <label htmlFor="tb-voice">Voice</label>
            <select id="tb-voice" value={choice.voice ?? ""} onChange={(e) => onChoice({ ...choice, voice: e.target.value || null })}>
              <option value="">Auto ({voiceLabel(autoVoiceFor(speakerId))})</option>
              {choice.voice && !voices.some((v) => v.value === choice.voice) && <option value={choice.voice}>{choice.voice}</option>}
              {voices.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
            </select>
            <label htmlFor="tb-avatar">Avatar</label>
            <select id="tb-avatar" value={choice.face ?? ""} onChange={(e) => onChoice({ ...choice, face: e.target.value || null })}>
              <option value="">Auto</option>
              <option value="none">None</option>
              {choice.face && choice.face !== "none" && !faces.some((f) => f.id === choice.face) && <option value={choice.face}>(removed face)</option>}
              {faces.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
            </select>
          </div>
          {novel && (
            <button className="spoiler-toggle" aria-pressed={spoilers} onClick={() => onSpoilers(!spoilers)}
                    title="Let answers use the whole book, including what comes later">
              Spoilers: {spoilers ? "on" : "off"}
            </button>
          )}
          {face && lipSyncable && (
            <label className="lipsync">
              <input type="checkbox" checked={lipSync} onChange={(e) => onLipSync(e.target.checked)} />
              Lip-synced face (answers start about 10 seconds later)
            </label>
          )}
        </div>

        <div className="tb-chat">
          <div className="tb-log" ref={log} role="log" aria-live="polite" aria-label="Conversation" tabIndex={0}>
            {talk.messages.length === 0 && <p className="empty-note">Nothing said yet. Ask {name} about the story so far.</p>}
            {talk.messages.map((m) => (
              <div key={m.id} className={`bubble ${m.role}`}>
                <span className="who-line">
                  {m.role === "user" ? "You" : speakerName(book, m.speakerId)}
                  {m.via === "typed" && m.role === "user" && <small> typed</small>}
                </span>
                <p>{m.text || (m.pending ? "…" : "")}</p>
              </div>
            ))}
          </div>
          <div className="tb-actions">
            <button className="link-btn" disabled={talk.messages.length === 0} onClick={talk.clearMessages}>Clear</button>
          </div>
          {talk.toast && (
            <div className="toast" role="alert">
              <span>{talk.toast}</span>
              <button onClick={talk.dismissToast} aria-label="Dismiss">Close</button>
            </div>
          )}
        </div>
      </div>

      <div className="tb-input">
        <button
          className={`mic-button${status === "listening" ? " live" : ""}`}
          aria-label="Hold to talk"
          aria-pressed={status === "listening"}
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
        <form className="tb-text" onSubmit={(e) => { e.preventDefault(); send(); }}>
          <label className="sr-only" htmlFor="tb-q">Type a question</label>
          <input id="tb-q" type="text" value={draft} maxLength={500} autoComplete="off" placeholder={`Ask ${name}…`}
                 onChange={(e) => setDraft(e.target.value)} />
          <button type="submit" disabled={!draft.trim()}>Send</button>
          <button type="button" onClick={talk.stop} disabled={!busy}>Stop</button>
        </form>
        <p className={`tb-status ${status}`} data-status={status}>{STATUS_TEXT[status]}</p>
        {status === "listening" && <div className="meter" aria-hidden="true"><span style={{ width: `${Math.min(100, talk.level * 600)}%` }} /></div>}
      </div>
    </section>
  );
}
