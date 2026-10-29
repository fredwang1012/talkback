import { useEffect, useRef, useState } from "react";
import type { Book, Face as FaceT } from "../api";
import type { TalkUi } from "../talk/useTalk";
import { PRESETS, type TalkChoice } from "./castLogic";
import { FaceView } from "./FaceView";
import { LiveFaceView } from "./LiveFaceView";
import type { LiveFaceUi } from "../talk/useLiveFace";
import { SourceLinks } from "./SourceLinks";
import RecapMenu, { type RecapChoice } from "./RecapMenu";
import GroupPicker from "./GroupPicker";
import { groupChoices, validPicks } from "./groupScene";
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
  choice: TalkChoice; // the Voice / Avatar pick of the character talked to (speakerId), #106
  onChoice: (c: TalkChoice) => void;
  voices: { value: string; label: string }[];
  spoilers: boolean;
  onSpoilers: (on: boolean) => void;
  portrait: string | null;
  onBack: () => void;
  onRecap: (c: RecapChoice) => void; // a recap is posted into this chat by the narrator
  onVoices: () => void;
  live?: LiveFaceUi; // #140: a live preset face (picker + video); absent or off keeps the usual faces
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
  idle: "Ready. Hold the microphone (or Space) to ask, or type below. Press it while the character talks to interrupt.",
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
  spoilers, onSpoilers, portrait, onBack, onRecap, onVoices, live,
}: Props) {
  const novel = showSpoilerToggle(book);
  const fullBook = novel && spoilers;
  const choices = speakerChoices(book, para, fullBook);
  const name = speakerName(book, speakerId);
  // a recap is read by the narrator: their face (lip-synced like an answer) and name show while it plays; in a group
  // round (#79) the character answering now does
  const round = !talk.narrating && talk.roundSpeaker && talk.roundSpeaker !== speakerId ? talk.roundSpeaker : null;
  const shownId = talk.narrating ? "narrator" : round ?? speakerId;
  const shownName = speakerName(book, shownId);
  const shownPortrait = talk.narrating || round === "narrator" ? book.narrator?.portrait ?? null
    : round ? book.characters.find((c) => c.id === round)?.portrait ?? null : portrait;
  const status = talkbackStatus(talk.state, talk.speaking); // follows the audio: the face uses it
  const line = useStableStatus(status, talk.state === "answering"); // the status text and the busy state
  const face = faceFor(shownId);
  const liveName = live?.info.avatars.find((a) => a.id === live.pick)?.name;
  const chapter = chapterAt(book, para);
  const voiceLabel = (v: string) => (PRESETS.includes(v) ? v : "your cloned voice");
  const [draft, setDraft] = useState("");
  // #79 group scene: others who answer the same question after `speakerId`, in the order picked (never in demo mode)
  const groupOptions = pinned ? [] : groupChoices(book, para, fullBook, speakerId);
  const [picks, setPicks] = useState<string[]>([]);
  const group = validPicks(picks, groupOptions);
  const groupKey = group.join("|");
  const setGroup = talk.setGroup;
  useEffect(() => { setGroup?.(groupKey ? groupKey.split("|") : [], (id) => speakerName(book, id)); }, [groupKey, book, setGroup]);
  useEffect(() => () => setGroup?.([], (id) => id), [setGroup]); // leaving Talkback: questions asked from the reader are one-to-one
  const askedNames = [name, ...group.map((id) => speakerName(book, id))].join(", ");
  const log = useRef<HTMLDivElement>(null);
  const held = useRef(false);

  useEffect(() => {
    const el = log.current;
    if (el) el.scrollTop = el.scrollHeight; // instant: no motion to reduce
  }, [talk.messages]);

  const press = () => { if (!held.current) { held.current = true; talk.pttDown(); } };
  const release = () => { if (held.current) { held.current = false; talk.pttUp(); } };
  // only what is given back to the character counts: a fresh session and the Gemini answer get the last HISTORY_TURNS lines
  const remembered = priorTurns(talk.messages, speakerId).length;
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
        <RecapMenu inSeries={!!book.series} textbook={book.kind === "textbook"} onPick={onRecap} />
        <button className="link-btn" onClick={onVoices}>Character Control</button>
        <div className={`badge${fullBook ? " spoilers-on" : ""}`}>
          <strong>You're at {chapter ?? `paragraph ${para + 1}`}.</strong>
          {fullBook && <> Spoilers on — whole book.</>}
        </div>
      </div>

      <div className="tb-main">
        <div className="tb-face-col">
          <div className={`tb-face${status === "listening" ? " live" : ""}${status === "thinking" ? " thinking" : ""}${status === "speaking" ? " speaking" : ""}`} aria-hidden="true">
            {live?.face && !talk.narrating ? <LiveFaceView media={live.media} name={liveName ?? shownName} />
              : face ? <FaceView face={face} speaking={talk.speaking} sync={talk.faceSync} clipShown={talk.clipShown} />
              : shownPortrait ? <img src={shownPortrait} alt="" />
              : <span className="mono">{shownName.charAt(0)}</span>}
          </div>
          <h2 className="tb-name">{shownName}</h2>

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
            <select id="tb-voice" value={choice.voice ?? ""} title={`${name}'s voice here (each character keeps their own)`} onChange={(e) => onChoice({ ...choice, voice: e.target.value || null })}>
              <option value="">Auto ({voiceLabel(autoVoiceFor(speakerId))})</option>
              {choice.voice && !voices.some((v) => v.value === choice.voice) && <option value={choice.voice}>{choice.voice}</option>}
              {voices.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
            </select>
            <label htmlFor="tb-avatar">Avatar</label>
            <select id="tb-avatar" value={choice.face ?? ""} title={`${name}'s face here (each character keeps their own)`} onChange={(e) => onChoice({ ...choice, face: e.target.value || null })}>
              <option value="">Auto</option>
              <option value="none">None</option>
              {choice.face && choice.face !== "none" && !faces.some((f) => f.id === choice.face) && <option value={choice.face}>(removed face)</option>}
              {faces.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
            </select>
            {live?.info.enabled && (
              <>
                <label htmlFor="tb-live">Live face</label>
                <select id="tb-live" value={live.pick} title="A Boson face that moves live with every word (instead of this character's face). Only Boson's own faces can be live."
                        onChange={(e) => live.setPick(e.target.value)}>
                  <option value="">Off</option>
                  {live.info.avatars.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
                </select>
              </>
            )}
          </div>
          <GroupPicker name={name} choices={groupOptions} picks={group} onPicks={setPicks} />
          {live?.status === "starting" && <p className="empty-note">Starting the live face…</p>}
          {live?.note && <p className="empty-note" role="status">{live.note}</p>}
          {novel && (
            <button className="spoiler-toggle" aria-pressed={spoilers} onClick={() => onSpoilers(!spoilers)}
                    title="Let answers use the whole book, including what comes later">
              Spoilers: {spoilers ? "on" : "off"}
            </button>
          )}
          {talk.setHandsFree && (
            <button className="spoiler-toggle hands-free" aria-pressed={!!talk.handsFree} onClick={() => talk.setHandsFree?.(!talk.handsFree)}
                    title="Recommended with headphones. Keeps the microphone open: speak to ask or to interrupt, a pause ends your question. Through speakers a little of the character's own voice comes back, so interrupting them takes clear, sustained speech.">
              Hands-free (use headphones): {talk.handsFree ? "on" : "off"}
            </button>
          )}
        </div>

        <div className="tb-chat">
          <div className="tb-log" ref={log} role="log" aria-live="polite" aria-label="Conversation" tabIndex={0}>
            {talk.messages.length === 0 && <p className="empty-note">Nothing said yet. Ask {name} about the story so far.</p>}
            {talk.messages.map((m) => m.role === "tool" ? (
              <div key={m.id} className="tool-chip" role="status">{m.text}</div>
            ) : (
              <div key={m.id} className={`bubble ${m.role}${m.interrupted ? " interrupted" : ""}`}>
                <span className="who-line">
                  {m.role === "user" ? "You" : m.role === "narrator" ? "Narrator" : speakerName(book, m.speakerId)}
                  {m.via === "typed" && m.role === "user" && <small> typed</small>}
                  {m.role === "narrator" && m.recap && <small> · Recap: {m.recap}</small>}
                  {m.interrupted && <small> · interrupted</small>}
                </span>
                <p>{m.text || (m.pending || m.loading ? "…" : "")}</p>
                {m.role === "character" && <SourceLinks sources={m.sources} suggestions={m.searchSuggestions} />}
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
            {talk.searching && <div className="tool-chip searching" role="status">Searching the web…</div>}
          </div>
          <div className="tb-actions">
            {remembered > 0 && (
              <span className="remembers" title="This chat is kept for this browser tab and its latest lines with this character are given back to them, so follow-up questions work">
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
          <input id="tb-q" type="text" value={draft} maxLength={500} autoComplete="off" placeholder={`Ask ${askedNames}…`}
                 onChange={(e) => setDraft(e.target.value)} />
          <button type="submit" disabled={!draft.trim()} title={group.length ? `${group.length + 1} characters answer in turn` : undefined}>
            {group.length ? `Send to ${group.length + 1}` : "Send"}
          </button>
          <button type="button" onClick={talk.stop} disabled={!busy}>Stop</button>
        </form>
        <p className={`tb-status ${line}`} data-status={line}>{STATUS_TEXT[line]}</p>
        {status === "listening" && <div className="meter" aria-hidden="true"><span style={{ width: `${Math.min(100, talk.level * 600)}%` }} /></div>}
      </div>
    </section>
  );
}
