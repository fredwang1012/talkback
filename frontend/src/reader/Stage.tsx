import { useEffect, useRef, useState } from "react";
import type { Book, Face as FaceT, Health } from "../api";
import { PRESETS, type TalkChoice } from "./castLogic";
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
  avatarSrc: string | null; // pre-rendered clip for the line being read, when there is one
  avatarPlaying: boolean;
  mode: "read" | "talk";
  onMode: (m: "read" | "talk") => void;
  pinned: string | null; // demo mode: talk only to this character
  needsCode: boolean;
  onCode: (code: string) => void;
  onOpenLab: () => void;
  faces: FaceT[]; // the face library
  voiceFor: (speakerId: string) => string; // the voice answers use (Talk choice, else the reading cast)
  autoVoiceFor: (speakerId: string) => string; // the reading-cast voice, shown beside "Auto"
  faceFor: (speakerId: string) => FaceT | undefined; // the face shown in Talk mode
  readingFace: FaceT | null; // face of whoever is reading aloud right now in read mode
  lipSyncable: boolean; // the shown face is bound to the answer voice, so the server can lip-sync it
  choice: TalkChoice;
  onChoice: (c: TalkChoice) => void;
  voices: { value: string; label: string }[]; // presets and cloned voices
  spoilers: boolean; // full-book mode (novels): answers may use the whole book
  onSpoilers: (on: boolean) => void;
  lipSync: boolean;
  onLipSync: (on: boolean) => void;
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

function Face({ src, name }: { src: string | null; name: string }) {
  return (
    <span className="face" aria-hidden="true">
      {src ? <img src={src} alt="" /> : name.charAt(0)}
    </span>
  );
}

/** A muted looping clip of a voice's face (speaking or idle). */
function FaceLoop({ src }: { src: string }) {
  return <video key={src} src={src} muted loop autoPlay playsInline preload="auto" />;
}

/** The face for the medallion: a lip-synced clip, else the speaking loop while speaking, else the idle loop or portrait. */
function FaceView({ face, speaking, clip }: { face: FaceT; speaking: boolean; clip: string | null }) {
  if (clip) return <video key={clip} src={clip} muted autoPlay playsInline />; // the audio element plays the sound
  if (speaking && face.talk) return <FaceLoop src={face.talk} />;
  if (face.idle) return <FaceLoop src={face.idle} />;
  return <img src={face.image} alt="" />;
}

/** Muted on purpose: the page's own audio element stays the single source of sound, so it cannot double up. */
function AvatarVideo({ src, playing }: { src: string; playing: boolean }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    if (playing) void v.play().catch(() => { /* autoplay refused: the portrait frame stays */ });
    else v.pause();
  }, [src, playing]);
  return <video ref={ref} key={src} src={src} muted playsInline preload="auto" />;
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

export default function Stage({ book, para, speakerId, onSpeaker, health, talk, audioAvailable, avatarSrc, avatarPlaying, mode, onMode, pinned, needsCode, onCode, onOpenLab, faces, voiceFor, autoVoiceFor, faceFor, readingFace, lipSyncable, choice, onChoice, voices, spoilers, onSpoilers, lipSync, onLipSync }: Props) {
  const [codeDraft, setCodeDraft] = useState("");
  const tutor = book.kind === "textbook";
  const fullBook = !tutor && spoilers;
  const { met, locked } = splitCharacters(book.characters, para, fullBook);
  const speaker = met.find((c) => c.id === speakerId);
  const name = speaker?.name ?? (tutor ? "Tutor" : "Narrator");
  const blurb = speaker ? speaker.persona : tutor ? "Explains the chapter in plain words. Ask about anything." : book.narrator?.is_character
    ? `Tells the story as ${book.characters.find((c) => c.id === book.narrator?.is_character)?.name ?? "a character"}.`
    : "Reads the text as written.";
  const live = talk.state === "listening";
  const portrait = speaker ? speaker.portrait : book.narrator?.portrait ?? null;
  // In read mode the reading cast's face is shown while it reads; otherwise the Talk avatar choice decides.
  const face = readingFace ?? faceFor(speakerId);
  const voiceLabel = (v: string) => (PRESETS.includes(v) ? v : "your cloned voice");
  const faceSpeaking = readingFace ? true : talk.speaking;
  const thinking = ["transcribing", "retrieving", "answering"].includes(talk.state) && !talk.speaking;
  const pressed = useRef(false); // survives re-renders between press and release

  const press = () => { if (!pressed.current) { pressed.current = true; talk.pttDown(); } };
  const release = () => { if (pressed.current) { pressed.current = false; talk.pttUp(); } };

  return (
    <aside className="stage" aria-label="Stage">
      <div className="mode" role="group" aria-label="Mode">
        <button aria-pressed={mode === "read"} onClick={() => onMode("read")}>Read</button>
        <button
          aria-pressed={mode === "talk"}
          disabled={!audioAvailable}
          title={audioAvailable ? "Press T to switch" : "Talk needs the Boson API key on the server"}
          onClick={() => onMode("talk")}
        >
          Talk
        </button>
      </div>

      <div className="speaker">
        <div className={`medallion${live ? " live" : ""}${thinking ? " thinking" : ""}${talk.speaking ? " speaking" : ""}`} aria-hidden="true">
          {avatarSrc ? <AvatarVideo src={avatarSrc} playing={avatarPlaying} />
            : face ? <FaceView face={face} speaking={faceSpeaking} clip={readingFace ? null : talk.clip} />
            : portrait ? <img src={portrait} alt="" /> : <span>{name.charAt(0)}</span>}
        </div>
        <div>
          <h3>{name}</h3>
          <p>{blurb}</p>
        </div>
      </div>
      {talk.speaking && <Waveform />}

      <div className={`badge${fullBook ? " spoilers-on" : ""}`}>
        {fullBook
          ? <><strong>Full book — spoilers on.</strong> {speaker ? name : "The narrator"} may reveal anything from the whole book.</>
          : <>
              <strong>{speaker ? `${name} knows` : "Story position:"}</strong> up to paragraph {para + 1}.
              {locked.length > 0 && <> {locked.length} more {locked.length === 1 ? "character appears" : "characters appear"} later.</>}
            </>}
      </div>
      {!tutor && (
        <button className="spoiler-toggle" aria-pressed={spoilers} onClick={() => onSpoilers(!spoilers)}
                title="Let answers use the whole book, including what comes later">
          Spoilers: {spoilers ? "on" : "off"}
        </button>
      )}

      <section aria-label="Who to talk to">
        <ul className="cast-list">
          <li>
            <button aria-pressed={speakerId === "narrator"} disabled={!!pinned} onClick={() => onSpeaker("narrator")}>
              <span className="who"><Face src={faceFor("narrator")?.image ?? book.narrator?.portrait ?? null} name={tutor ? "Tutor" : "Narrator"} />{tutor ? "Tutor" : "Narrator"}</span> <small>{voiceLabel(voiceFor("narrator"))}</small>
            </button>
          </li>
          {met.map((c) => (
            <li key={c.id}>
              <button aria-pressed={speakerId === c.id} disabled={!!pinned && pinned !== c.id} onClick={() => onSpeaker(c.id)}>
                <span className="who"><Face src={faceFor(c.id)?.image ?? c.portrait} name={c.name} />{c.name}</span> <small>{voiceLabel(voiceFor(c.id))}</small>
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
        <form className="code-form" onSubmit={(e) => { e.preventDefault(); if (codeDraft.trim()) onCode(codeDraft.trim()); }}>
          <label htmlFor="access-code">This server needs an access code for voices</label>
          <div>
            <input id="access-code" type="password" autoComplete="off" value={codeDraft} onChange={(e) => setCodeDraft(e.target.value)} />
            <button type="submit">Unlock voices</button>
          </div>
        </form>
      )}
      <div className="talk-pick">
        <label htmlFor="talk-voice">Voice</label>
        <select id="talk-voice" value={choice.voice ?? ""} onChange={(e) => onChoice({ ...choice, voice: e.target.value || null })}>
          <option value="">Auto ({voiceLabel(autoVoiceFor(speakerId))})</option>
          {choice.voice && !voices.some((v) => v.value === choice.voice) && <option value={choice.voice}>{choice.voice}</option>}
          {voices.map((v) => <option key={v.value} value={v.value}>{v.label}</option>)}
        </select>
        <label htmlFor="talk-avatar">Avatar</label>
        <select id="talk-avatar" value={choice.face ?? ""} onChange={(e) => onChoice({ ...choice, face: e.target.value || null })}>
          <option value="">Auto</option>
          <option value="none">None</option>
          {choice.face && choice.face !== "none" && !faces.some((f) => f.id === choice.face) && <option value={choice.face}>(removed face)</option>}
          {faces.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
        </select>
      </div>
      <button className="link-btn" onClick={onOpenLab}>Voices, faces and cloning</button>
      {face && lipSyncable && (
        <label className="lipsync">
          <input type="checkbox" checked={lipSync} onChange={(e) => onLipSync(e.target.checked)} />
          Lip-synced face (answers start about 10 seconds later)
        </label>
      )}

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
