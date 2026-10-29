import { useState } from "react";
import type { Book, Face as FaceT, Health } from "../api";
import type { TalkUi } from "../talk/useTalk";
import AskBar from "./AskBar";
import { splitCharacters } from "./position";

type Props = {
  book: Book;
  para: number;
  speakerId: string;
  onSpeaker: (id: string) => void; // "Talk to" picks who answers while reading (#34); no switch to Talkback
  health: Health | null;
  talk: TalkUi;
  audioAvailable: boolean;
  onMode: (m: "read" | "talk") => void; // "Open Talkback" link
  pinned: string | null; // demo mode: talk only to this character
  needsCode: boolean;
  onCode: (code: string, remember: boolean) => void;
  onOpenLab: () => void;
  faceFor: (speakerId: string) => FaceT | undefined; // the face an answer shows (the cast list's little faces)
};

function Face({ src, name }: { src: string | null; name: string }) {
  return (
    <span className="face" aria-hidden="true">
      {src ? <img src={src} alt="" /> : name.charAt(0)}
    </span>
  );
}

function Lock() {
  return (
    <svg className="lock" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-label="Locked">
      <rect x="5" y="11" width="14" height="9" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" />
    </svg>
  );
}

function Waveform() {
  return (
    <div className="wave" role="img" aria-label="Speaking">
      {Array.from({ length: 9 }, (_, i) => <span key={i} style={{ animationDelay: `${i * 90}ms` }} />)}
    </div>
  );
}

/**
 * The read-mode side panel: who answers questions asked while reading (#34) and the ask bar. The face itself lives in
 * the avatar player (#33, reader/AvatarPlayer), so its videos are mounted once.
 */
export default function Stage({ book, para, speakerId, onSpeaker, health, talk, audioAvailable, onMode, pinned, needsCode, onCode, onOpenLab, faceFor }: Props) {
  const [codeDraft, setCodeDraft] = useState("");
  const [remember, setRemember] = useState(true);
  const tutor = book.kind === "textbook";
  // read mode (#94): the Talkback spoiler setting does not apply here, so unmet characters stay locked and no
  // spoiler note shows; Talkback has its own
  const { met, locked } = splitCharacters(book.characters, para, false);
  const speaker = met.find((c) => c.id === speakerId);
  const name = speaker?.name ?? (tutor ? "Tutor" : "Narrator");
  const blurb = speaker ? speaker.persona : tutor ? "Explains the chapter in plain words. Ask about anything." : book.narrator?.is_character
    ? `Tells the story as ${book.characters.find((c) => c.id === book.narrator?.is_character)?.name ?? "a character"}.`
    : "Reads the text as written.";
  const talkTo = (id: string) => onSpeaker(id); // #34: they answer the next question, reading goes on

  return (
    <aside className="stage" aria-label="Stage">
      <div className="speaker">
        <div>
          <h3>{name}</h3>
          <p>{blurb}</p>
        </div>
      </div>
      {talk.speaking && <Waveform />}

      <div className="badge">
        <strong>Story position:</strong> paragraph {para + 1}.
        {locked.length > 0 && <> {locked.length} more {locked.length === 1 ? "character appears" : "characters appear"} later.</>}
      </div>
      <section aria-label="Who to talk to">
        <ul className="cast-list">
          <li>
            <button className="talk-to" disabled={!audioAvailable || !!pinned} onClick={() => talkTo("narrator")} aria-pressed={speakerId === "narrator"}
                    title={audioAvailable ? undefined : "Talkback needs the Boson API key on the server"}>
              <Face src={faceFor("narrator")?.image ?? book.narrator?.portrait ?? null} name={tutor ? "Tutor" : "Narrator"} />
              <span className="who-text"><strong>Talk to {tutor ? "the Tutor" : "the Narrator"}</strong>
                <small>{tutor ? "Explains the chapter in plain words" : "Tells the story"}</small></span>
            </button>
          </li>
          {met.map((c) => (
            <li key={c.id}>
              <button className="talk-to" disabled={!audioAvailable || (!!pinned && pinned !== c.id)} onClick={() => talkTo(c.id)} aria-pressed={speakerId === c.id}>
                <Face src={faceFor(c.id)?.image ?? c.portrait} name={c.name} />
                <span className="who-text"><strong>Talk to {c.name}</strong><small>{c.persona}</small></span>
              </button>
            </li>
          ))}
          {locked.map((c) => (
            <li key={c.id}>
              <button disabled title="Not met yet: they appear later in the story">
                <span className="who"><Lock />{c.name}</span> <small>appears later</small>
              </button>
            </li>
          ))}
        </ul>
        {book.characters.length === 0 && (
          <p className="empty-note">No cast yet. Characters appear here once the casting step has run.</p>
        )}
      </section>

      {needsCode && (
        <form className="code-form" onSubmit={(e) => { e.preventDefault(); if (codeDraft.trim()) onCode(codeDraft.trim(), remember); }}>
          <label htmlFor="access-code">This server needs an access code for voices</label>
          <div>
            <input id="access-code" type="password" autoComplete="off" value={codeDraft} onChange={(e) => setCodeDraft(e.target.value)} />
            <button type="submit">Unlock voices</button>
          </div>
          <label className="remember"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> Remember on this device</label>
        </form>
      )}
      <button className="link-btn" onClick={onOpenLab}>Voices, faces and cloning</button>
      {audioAvailable && <button className="link-btn" onClick={() => onMode("talk")}>Open Talkback, a full conversation view</button>}
      <AskBar talk={talk} name={name} audioAvailable={audioAvailable}
              unavailableNote={health?.boson_key_set === false || !health ? "Voice questions need the Boson API key on the server." : undefined} />

      {talk.question && (
        <p className="question">
          {talk.language && <span className="lang" title="Detected from what you said">{talk.language.name}</span>}
          “{talk.question}”
        </p>
      )}
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
