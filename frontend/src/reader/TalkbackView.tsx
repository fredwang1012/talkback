import { useEffect, useRef, useState } from "react";
import type { Book, Face as FaceT } from "../api";
import type { TalkUi } from "../talk/useTalk";
import { PRESETS, type TalkChoice } from "./castLogic";
import { FaceView } from "./FaceView";
import RecapMenu, { type RecapChoice } from "./RecapMenu";
import { priorTurns } from "../talk/chatLog";
import { chapterAt, createStatusGate, showSpoilerToggle, speakerChoices, speakerName, talkbackStatus, type TalkbackStatus } from "./talkbackLogic";

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
  onRecap: (c: RecapChoice) => void; // a recap is posted into this chat by the narrator
  onVoices: () => void;
};

/** The status line, held still between the sentences of an answer (createStatusGate, #81). */
function useStableStatus(raw: TalkbackStatus, answering: boolean): TalkbackStatus {
  const [shown, setShown] = useState(raw);
  const gate = useRef<ReturnType<typeof createStatusGate> | null>(null);
  if (!gate.current) gate.current = createStatusGate(setShown);
  useEffect(() => { gate.current?.update(raw, answering); }, [raw, answering]);
  useEffect(() => () => gate.current?.dispose(), []);
  return shown;
}

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
  spoilers, onSpoilers, lipSyncable, lipSync, onLipSync, portrait, onBack, onRecap, onVoices,
}: Props) {
  const novel = showSpoilerToggle(book);
  const fullBook = novel && spoilers;
  const choices = speakerChoices(book, para, fullBook);
  const name = speakerName(book, speakerId);
  const status = talkbackStatus(talk.state, talk.speaking); // follows the audio: the face uses it
  const line = useStableStatus(status, talk.state === "answering"); // the status text and the busy state
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
  const remembered = priorTurns(talk.messages, speakerId, 99).length;
  const busy = line === "thinking" || line === "speaking";

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
        <RecapMenu inSeries={!!book.series} onPick={onRecap} />
        <button className="link-btn" onClick={onVoices}>Voices</button>
        <div className={`badge${fullBook ? " spoilers-on" : ""}`}>
          <strong>You're at {chapter ?? `paragraph ${para + 1}`}.</strong>
          {fullBook && <> Spoilers on — whole book.</>}
        </div>
      </div>

      <div className="tb-main">
        <div className="tb-face-col">
          <div className={`tb-face${status === "listening" ? " live" : ""}${status === "thinking" ? " thinking" : ""}${status === "speaking" ? " speaking" : ""}`} aria-hidden="true">
            {face ? <FaceView face={face} speaking={talk.speaking} sync={talk.faceSync} clipShown={talk.clipShown} />
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
              Lip-synced face (answers start about 2 seconds later)
            </label>
          )}
        </div>

        <div className="tb-chat">
          <div className="tb-log" ref={log} role="log" aria-live="polite" aria-label="Conversation" tabIndex={0}>
            {talk.messages.length === 0 && <p className="empty-note">Nothing said yet. Ask {name} about the story so far.</p>}
            {talk.messages.map((m) => (
              <div key={m.id} className={`bubble ${m.role}`}>
                <span className="who-line">
                  {m.role === "user" ? "You" : m.role === "narrator" ? "Narrator" : speakerName(book, m.speakerId)}
                  {m.via === "typed" && m.role === "user" && <small> typed</small>}
                  {m.role === "narrator" && m.recap && <small> · Recap: {m.recap}</small>}
                </span>
                <p>{m.text || (m.pending || m.loading ? "…" : "")}</p>
                {m.role !== "user" && (m.id === talk.liveId ? !!m.text : !m.pending && talk.replayable?.has(m.id)) && (() => {
                  const on = talk.replayingId === m.id;
                  const verb = on ? "Pause" : talk.isPaused?.(m.id) ? "Resume" : "Play";
                  const what = m.role === "narrator" ? "the narrator's recap"
                    : `${speakerName(book, m.speakerId)}'s reply${m.id === talk.liveId ? "" : " again"}`;
                  return (
                    <button type="button" className="replay-btn" aria-pressed={on} aria-label={`${verb} ${what}`}
                            onClick={() => talk.replay(m.id)}>
                      {on ? "❚❚" : "▶"}
                    </button>
                  );
                })()}
              </div>
            ))}
          </div>
          <div className="tb-actions">
            {remembered > 0 && (
              <span className="remembers" title="This chat is kept for this browser tab and given back to the character, so follow-up questions work">
                {name} remembers this chat ({remembered} {remembered === 1 ? "line" : "lines"})
              </span>
            )}
            <button className="link-btn" disabled={talk.messages.length === 0} onClick={talk.clearMessages}
                    title="Delete this chat; the character forgets it too">Clear chat</button>
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
        <p className={`tb-status ${line}`} data-status={line}>{STATUS_TEXT[line]}</p>
        {status === "listening" && <div className="meter" aria-hidden="true"><span style={{ width: `${Math.min(100, talk.level * 600)}%` }} /></div>}
      </div>
    </section>
  );
}
