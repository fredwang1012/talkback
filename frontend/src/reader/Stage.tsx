import { createClipFollower, type FollowAudio } from "./clipFollow";
import { tryPlayQuietly } from "../audio/mediaErrors";
import { useEffect, useRef, useState } from "react";
import type { Book, Face as FaceT, Health } from "../api";
import type { FaceSync } from "../talk/faceSync";
import type { TalkUi } from "../talk/useTalk";
import type { TalkState } from "../talk/TalkController";
import { splitCharacters } from "./position";
import { FaceView, ReadFace } from "./FaceView";
import { readLipSyncCost } from "./readLipSync";

/** Read-mode lip-sync (#80): the toggle (offered only when a face on screen can be lip-synced) and the persistent
 * clip video the sync drives; `shown` once a clip has a frame and is in step with the voice. */
export type ReadLipSyncUi = {
  available: boolean;
  on: boolean;
  onToggle: (on: boolean) => void;
  sync: FaceSync;
  shown: boolean;
};

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
  getAudio?: () => FollowAudio | null; // read mode: the chunk's audio element, which the clip follows
  onMode: (m: "read" | "talk") => void; // "Talkback" link
  pinned: string | null; // demo mode: talk only to this character
  needsCode: boolean;
  onCode: (code: string, remember: boolean) => void;
  onOpenLab: () => void;
  faceFor: (speakerId: string) => FaceT | undefined; // the face shown in Talk mode
  readingFace: FaceT | null; // face of whoever is reading aloud right now in read mode
  preloadLoops?: string[]; // read mode: the next reader's loop videos, warmed up so a speaker change starts on time
  spoilers: boolean; // full-book mode (novels): answers may use the whole book
  readSpeaking: boolean; // read mode: the audio is audibly speaking right now (not in a pause)
  levelRef: { current: number }; // read mode: live loudness of the audio, for the still-image pulse
  readLipSync?: ReadLipSyncUi;
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

/**
 * Muted on purpose: the page's own audio element stays the single source of sound, so it cannot double up.
 * The clip is driven by the chunk's audio (reader/clipFollow): it starts, pauses and ends with the voice and
 * corrects drift by nudging its playback rate rather than seeking.
 */
function AvatarVideo({ src, playing, getAudio }: { src: string; playing: boolean; getAudio?: () => FollowAudio | null }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    if (!getAudio) { // no audio to follow (silent preview): plain play/pause
      if (playing) void tryPlayQuietly(v); else v.pause();
      return;
    }
    const f = createClipFollower(v);
    const h = window.setInterval(() => { if (playing) f.tick(getAudio()); else if (!v.paused) v.pause(); }, 100);
    return () => window.clearInterval(h);
  }, [src, playing, getAudio]);
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

/** The one persistent read-mode lip-sync <video>, owned by the sync (see talk/faceSync): never swapped, so nothing
 * flashes; it sits over the loop face (or the portrait) and fades in only while a clip plays in step. It stays
 * mounted whatever face is shown, so a chunk's clip is never lost to a face change between chunks. */
function ReadClipVideo({ sync, shown }: { sync: FaceSync; shown: boolean }) {
  const ref = useRef<HTMLVideoElement>(null);
  useEffect(() => {
    sync.setVideo(ref.current);
    return () => sync.setVideo(null);
  }, [sync]);
  return <video ref={ref} className={`rf-clip${shown ? " on" : ""}`} muted playsInline />;
}

export default function Stage({ book, para, speakerId, onSpeaker, health, talk, audioAvailable, avatarSrc, avatarPlaying, getAudio, onMode, pinned, needsCode, onCode, onOpenLab, faceFor, readingFace, preloadLoops = [], spoilers, readSpeaking, levelRef, readLipSync }: Props) {
  const [codeDraft, setCodeDraft] = useState("");
  const [remember, setRemember] = useState(true);
  const clipShown = !!readLipSync?.shown;
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
  const faceSpeaking = readingFace ? true : talk.speaking;
  const big = !!face || !!avatarSrc || clipShown; // a real face grows the circle (animated); no face keeps the small monogram
  const thinking = ["transcribing", "retrieving", "answering"].includes(talk.state) && !talk.speaking;
  const talkTo = (id: string) => { onSpeaker(id); onMode("talk"); };
  const pressed = useRef(false); // survives re-renders between press and release

  const press = () => { if (!pressed.current) { pressed.current = true; talk.pttDown(); } };
  const release = () => { if (pressed.current) { pressed.current = false; talk.pttUp(); } };

  return (
    <aside className="stage" aria-label="Stage">
      <div className={`speaker${big ? " big" : ""}`}>
        <div className={`medallion${big ? " big" : ""}${live ? " live" : ""}${thinking ? " thinking" : ""}${talk.speaking ? " speaking" : ""}`} aria-hidden="true">
          {avatarSrc ? <AvatarVideo src={avatarSrc} playing={avatarPlaying} getAudio={getAudio} />
            : face && readingFace ? <ReadFace face={face} speaking={readSpeaking} levelRef={levelRef} clipShown={clipShown} />
            : face ? <FaceView face={face} speaking={faceSpeaking} sync={talk.faceSync} clipShown={talk.clipShown} />
            : portrait ? <img src={portrait} alt="" /> : <span>{name.charAt(0)}</span>}
          {readLipSync && <ReadClipVideo sync={readLipSync.sync} shown={clipShown} />}
          {preloadLoops.map((src) => <video key={src} className="preload" src={src} muted playsInline preload="auto" />)}
        </div>
        <div>
          <h3>{name}</h3>
          <p>{blurb}</p>
        </div>
      </div>
      {readLipSync?.available && (
        <label className="lipsync">
          <input type="checkbox" checked={readLipSync.on} onChange={(e) => readLipSync.onToggle(e.target.checked)} />
          <span>Lip-sync while reading <small>{readLipSyncCost()} Each line starts a moment later.</small></span>
        </label>
      )}
      {talk.speaking && <Waveform />}

      <div className={`badge${fullBook ? " spoilers-on" : ""}`}>
        {fullBook
          ? <><strong>Full book — spoilers on.</strong> {speaker ? name : "The narrator"} may reveal anything from the whole book.</>
          : <>
              <strong>{speaker ? `${name} knows` : "Story position:"}</strong> up to paragraph {para + 1}.
              {locked.length > 0 && <> {locked.length} more {locked.length === 1 ? "character appears" : "characters appear"} later.</>}
            </>}
      </div>
      <section aria-label="Who to talk to">
        <ul className="cast-list">
          <li>
            <button className="talk-to" disabled={!audioAvailable || !!pinned} onClick={() => talkTo("narrator")}
                    title={audioAvailable ? undefined : "Talkback needs the Boson API key on the server"}>
              <Face src={faceFor("narrator")?.image ?? book.narrator?.portrait ?? null} name={tutor ? "Tutor" : "Narrator"} />
              <span className="who-text"><strong>Talk to {tutor ? "the Tutor" : "the Narrator"}</strong>
                <small>{tutor ? "Explains the chapter in plain words" : "Tells the story"}</small></span>
            </button>
          </li>
          {met.map((c) => (
            <li key={c.id}>
              <button className="talk-to" disabled={!audioAvailable || (!!pinned && pinned !== c.id)} onClick={() => talkTo(c.id)}>
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
