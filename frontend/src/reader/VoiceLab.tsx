import { micErrorMessage } from "../audio/mediaErrors";
import { useRef, useState, type Dispatch, type SetStateAction } from "react";
import { animateFace, cloneVoice, deleteFace, deleteVoice, uploadFace, type Book, type Cast, type Face } from "../api";
import {
  PRESETS, bookVoice, castRows, castFace, castVoice, PITCH_SPEED, BOOK_TAGS, voiceSummary, tagLabel, effectiveMode, isNovel, oneVoiceOverride, runAnimate, withAssignment, type ReadingStyle,
} from "./castLogic";

export { PRESETS };
const SCRIPT =
  "Well, it is a rather long story, but I have to tell you that I was walking home late one evening " +
  "when I noticed a light burning in the window of the old house.";

type Props = {
  book: Book;
  cast: Cast;
  onCast: (cast: Cast) => void; // a changed cast; the parent saves it
  castError: string | null;
  style: ReadingStyle;
  onStyle: (s: ReadingStyle) => void;
  cloned: Record<string, string>; // name -> voice id of the visitor's cloned voices
  onCloned: Dispatch<SetStateAction<Record<string, string>>>;
  animating: Set<string>; // animation state is owned by the reader so it survives closing this panel
  setAnimating: Dispatch<SetStateAction<Set<string>>>;
  animErrors: Record<string, string>;
  setAnimErrors: Dispatch<SetStateAction<Record<string, string>>>;
  faces: Face[];
  onFaces: Dispatch<SetStateAction<Face[]>>;
  onRemoved: (gone: { voice?: string; face?: string }) => void; // browser-side choices pointing at it go back to Auto
  onFacesChanged: () => void; // a face was deleted: the cast may have lost a reference
  onPreview: (speaker: string, voice: string) => void;
  audioAvailable: boolean;
  onClose: () => void;
};

/** Cast panel (a voice per speaker) and Voice lab (clone your own voice, with consent). */
export default function VoiceLab({ book, cast, onCast, castError, style, onStyle, cloned, onCloned, animating, setAnimating, animErrors, setAnimErrors, faces, onFaces, onFacesChanged, onRemoved, onPreview, audioAvailable, onClose }: Props) {
  const [transcript, setTranscript] = useState(SCRIPT);
  const [name, setName] = useState("");
  const [consent, setConsent] = useState(false);
  const [clip, setClip] = useState<{ blob: Blob; filename: string } | null>(null);
  const [face, setFace] = useState<File | null>(null);
  const [recording, setRecording] = useState(false);
  const [status, setStatus] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);
  const [faceName, setFaceName] = useState("");
  const [faceFile, setFaceFile] = useState<File | null>(null);
  const [faceBusy, setFaceBusy] = useState(false);
  const [faceMsg, setFaceMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);

  const [confirming, setConfirming] = useState<string | null>(null); // "face:<id>" or "voice:<id>"
  const [voiceMsg, setVoiceMsg] = useState<string | null>(null);

  async function removeVoice(name: string, id: string) {
    setConfirming(null);
    setVoiceMsg(null);
    try {
      await deleteVoice(id);
      onCloned(({ [name]: _x, ...rest }) => { void _x; return rest; });
      onRemoved({ voice: id });
      onFacesChanged(); // its linked face is gone too, and casts using either fall back to Auto
    } catch (e) {
      setVoiceMsg(e instanceof Error ? e.message : String(e));
    }
  }

  async function startRecording() {
    setStatus(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const parts: Blob[] = [];
      rec.ondataavailable = (e) => parts.push(e.data);
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(parts, { type: rec.mimeType || "audio/webm" });
        setClip({ blob, filename: `recording.${(rec.mimeType || "audio/webm").includes("ogg") ? "ogg" : "webm"}` });
        setRecording(false);
      };
      recorder.current = rec;
      rec.start();
      setRecording(true);
    } catch (e) {
      setStatus({ kind: "error", text: `${micErrorMessage(e)} Or upload a file instead.` });
    }
  }

  async function submit() {
    if (!clip) return;
    setBusy(true);
    setStatus(null);
    try {
      const out = await cloneVoice(clip.blob, clip.filename, transcript, name.trim(), consent, face);
      onCloned((c) => ({ ...c, [out.name]: out.voice }));
      if (out.face) onFacesChanged();
      setStatus({ kind: "ok", text: `Created "${out.name}". Pick it for any speaker in the voice lists above.` });
    } catch (e) {
      setStatus({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  const mode = effectiveMode(book, style);
  const oneVoice = oneVoiceOverride(book, cast, { ...style, mode: "one" }) ?? "marcus";
  const rows = castRows(book, cast);

  async function submitFace() {
    if (!faceFile) return;
    setFaceBusy(true);
    setFaceMsg(null);
    try {
      const f = await uploadFace(faceFile, faceName.trim());
      onFaces((list) => [...list, f]);
      setFaceName("");
      setFaceFile(null);
      setFaceMsg({ kind: "ok", text: `Saved "${f.name}". Pick it from any face list.` });
    } catch (e) {
      setFaceMsg({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setFaceBusy(false);
    }
  }
  async function animate(id: string) {
    const out = await runAnimate(id, animating, {
      onStart: (i) => { setAnimating((s) => new Set(s).add(i)); setAnimErrors(({ [i]: _x, ...rest }) => { void _x; return rest; }); },
      onEnd: (i) => setAnimating((s) => { const n = new Set(s); n.delete(i); return n; }),
      onError: (i, m) => setAnimErrors((e) => ({ ...e, [i]: m })),
    }, animateFace);
    if (out) onFaces((list) => list.map((f) => (f.id === id ? { id: out.id, name: out.name, image: out.image, voice: out.voice, talk: out.talk, idle: out.idle } : f)));
  }
  async function remove(f: Face) {
    setConfirming(null);
    try {
      await deleteFace(f.id);
      onRemoved({ face: f.id });
      onFaces((list) => list.filter((x) => x.id !== f.id));
      onFacesChanged();
    } catch (e) {
      setAnimErrors((m) => ({ ...m, [f.id]: e instanceof Error ? e.message : String(e) }));
    }
  }
  const faceOptions = (value: string | null) => (
    <>
      <option value="">None</option>
      {value && !faces.some((f) => f.id === value) && <option value={value}>(removed face)</option>}
      {faces.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
    </>
  );
  const voiceOptions = (value: string, withValue = true) => (
    <>
      {withValue && !options.some((o) => o.value === value) && <option value={value}>{value}</option>}
      {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
    </>
  );
  const options = [...PRESETS.map((v) => ({ value: v, label: v })), ...Object.entries(cloned).map(([n, v]) => ({ value: v, label: `${n} (cloned)` }))];
  const canSubmit = !!clip && consent && name.trim().length > 0 && transcript.trim().length > 0 && !busy && audioAvailable;

  return (
    <div className="lab" role="dialog" aria-label="Voices">
      <div className="lab-head">
        <h2>Voices</h2>
        <button onClick={onClose}>Close</button>
      </div>

      {isNovel(book) && (
        <section aria-label="Reading style">
          <h3>Reading style</h3>
          <div className="mode" role="group" aria-label="Reading style">
            <button aria-pressed={style.mode === "one"} onClick={() => onStyle({ ...style, mode: "one" })}>One voice</button>
            <button aria-pressed={style.mode === "cast"} onClick={() => onStyle({ ...style, mode: "cast" })}>Full cast</button>
          </div>
        </section>
      )}

      <section aria-label="Voices and faces for reading">
        <h3>{mode === "one" ? "Voice for the whole book" : isNovel(book) ? "Cast" : "Narrator"}</h3>
        {mode === "one" ? (
          <ul className="lab-cast">
            <li>
              <label htmlFor="one-voice">Voice</label>
              <select id="one-voice" value={oneVoice} onChange={(e) => onStyle({ ...style, voice: e.target.value })}>
                {voiceOptions(oneVoice)}
              </select>
              <button disabled={!audioAvailable} onClick={() => onPreview("narrator", oneVoice)}>Preview</button>
            </li>
            <li>
              <label htmlFor="one-face">Face</label>
              <select id="one-face" value={style.face ?? ""} onChange={(e) => onStyle({ ...style, face: e.target.value || null })}>
                {faceOptions(style.face)}
              </select>
              <span />
            </li>
          </ul>
        ) : (
          <ul className="lab-cast">
            {rows.map((r) => {
              const value = (r.id === "narrator" ? cast.narrator : cast.characters[r.id])?.voice ?? "";
              return (
                <li key={r.id} className="cast-row">
                  <span className="cast-name">{r.name}<small className="cast-uses"> uses {voiceSummary(book, cast, r.id)}</small></span>
                  <label className="sr-only" htmlFor={`voice-${r.id}`}>{r.name} voice</label>
                  <select id={`voice-${r.id}`} value={value} onChange={(e) => onCast(withAssignment(cast, r.id, { voice: e.target.value || null }))}>
                    <option value="">Book default ({bookVoice(book, r.id)})</option>
                    {voiceOptions(value, false)}
                  </select>
                  {r.id === "narrator" ? <span /> : (
                    <>
                      <label className="sr-only" htmlFor={`tags-${r.id}`}>{r.name} pitch and speed</label>
                      <select id={`tags-${r.id}`} value={cast.characters[r.id]?.tags ?? BOOK_TAGS}
                        onChange={(e) => onCast(withAssignment(cast, r.id, { tags: e.target.value === BOOK_TAGS ? null : e.target.value }))}>
                        <option value={BOOK_TAGS}>Book default ({tagLabel(book.characters.find((c) => c.id === r.id)?.voice_tags)})</option>
                        {PITCH_SPEED.map((t) => <option key={t.value || "none"} value={t.value}>{t.label}</option>)}
                      </select>
                    </>
                  )}
                  <label className="sr-only" htmlFor={`face-${r.id}`}>{r.name} face</label>
                  <select id={`face-${r.id}`} value={castFace(cast, r.id) ?? ""} onChange={(e) => onCast(withAssignment(cast, r.id, { face: e.target.value || null }))}>
                    {faceOptions(castFace(cast, r.id))}
                  </select>
                  <button disabled={!audioAvailable} onClick={() => onPreview(r.id, castVoice(book, cast, r.id))} aria-label={`Preview ${r.name}`}>Preview</button>
                </li>
              );
            })}
          </ul>
        )}
        {mode === "cast" && isNovel(book) && rows.length === 1 && (
          <p className="empty-note">No main characters yet. They appear here once the casting step has run.</p>
        )}
        {castError && <p role="alert" className="lab-error">{castError}</p>}
        {!audioAvailable && <p className="empty-note">Previews and cloning need the Boson API key on the server.</p>}
      </section>

      <section aria-label="Faces">
        <h3>Faces</h3>
        <p className="empty-note">Upload any portrait. Animating gives it a talking and an idle loop; otherwise it shows as a still picture.</p>
        {faces.length > 0 && (
          <ul className="face-list">
            {faces.map((f) => {
              const busy = animating.has(f.id);
              const done = !!f.talk && !!f.idle;
              return (
                <li key={f.id}>
                  <span className="face"><img src={f.image} alt="" /></span>
                  <span className="face-name">{f.name}</span>
                  <button disabled={busy || done} aria-busy={busy} onClick={() => void animate(f.id)}
                          title={done ? "Already animated" : "Renders a talking and an idle loop"}>
                    {busy ? "Animating… (1 to 2 min)" : done ? "Animated" : "Animate (about $0.05)"}
                  </button>
                  {confirming === `face:${f.id}` ? (
                    <span role="alertdialog" aria-label={`Delete ${f.name}?`}>
                      Delete this face for good?{" "}
                      <button onClick={() => void remove(f)}>Delete forever</button>{" "}
                      <button autoFocus onClick={() => setConfirming(null)}>Cancel</button>
                    </span>
                  ) : (
                    <button disabled={busy} onClick={() => setConfirming(`face:${f.id}`)} aria-label={`Delete ${f.name}`}>Delete</button>
                  )}
                  {animErrors[f.id] && <p role="alert" className="lab-error face-error">{animErrors[f.id]}</p>}
                </li>
              );
            })}
          </ul>
        )}
        <label htmlFor="face-name">Face name</label>
        <input id="face-name" value={faceName} maxLength={60} onChange={(e) => setFaceName(e.target.value)} />
        <label className="file" htmlFor="face-file">
          Image (PNG, JPEG or WebP, up to 5 MB)
          <input id="face-file" type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => setFaceFile(e.target.files?.[0] ?? null)} />
        </label>
        <button className="primary" disabled={!faceFile || !faceName.trim() || faceBusy} onClick={() => void submitFace()}>{faceBusy ? "Uploading…" : "Upload a face"}</button>
        {faceMsg && <p role={faceMsg.kind === "error" ? "alert" : "status"} className={`lab-${faceMsg.kind}`}>{faceMsg.text}</p>}
      </section>

      <section aria-label="Your cloned voices">
        <h3>Your cloned voices</h3>
        {Object.keys(cloned).length === 0 && <p className="empty-note">No cloned voices yet.</p>}
        <ul className="face-list">
          {Object.entries(cloned).map(([n, id]) => (
            <li key={id}>
              <span className="face-name">{n}</span>
              {confirming === `voice:${id}` ? (
                <span role="alertdialog" aria-label={`Delete ${n}?`}>
                  Delete this voice (and its face) for good? Casts using it go back to Auto.{" "}
                  <button onClick={() => void removeVoice(n, id)}>Delete forever</button>{" "}
                  <button autoFocus onClick={() => setConfirming(null)}>Cancel</button>
                </span>
              ) : (
                <button onClick={() => setConfirming(`voice:${id}`)} aria-label={`Delete voice ${n}`}>Delete</button>
              )}
            </li>
          ))}
        </ul>
        {voiceMsg && <p role="alert" className="lab-error">{voiceMsg}</p>}
      </section>

      <section>
        <h3>Your voice</h3>
        <p className="empty-note">Read the line below aloud for 5 to 30 seconds in a quiet room, then check that the text matches what you said.</p>
        <label htmlFor="lab-transcript">What you will say</label>
        <textarea id="lab-transcript" rows={3} value={transcript} onChange={(e) => setTranscript(e.target.value)} />
        <div className="lab-row">
          <button onClick={recording ? () => recorder.current?.stop() : startRecording}>{recording ? "Stop recording" : "Record"}</button>
          <label className="file">
            Or upload a file
            <input type="file" accept="audio/*" onChange={(e) => { const f = e.target.files?.[0]; if (f) setClip({ blob: f, filename: f.name }); }} />
          </label>
        </div>
        {clip && <p className="empty-note">Ready: {clip.filename} ({Math.round(clip.blob.size / 1024)} KB)</p>}
        <label className="file">
          Add a face (optional): a portrait shown whenever this voice speaks
          <input type="file" accept="image/png,image/jpeg,image/webp" onChange={(e) => setFace(e.target.files?.[0] ?? null)} />
        </label>
        {face && <p className="empty-note">Face: {face.name}</p>}
        <label htmlFor="lab-name">Name this voice</label>
        <input id="lab-name" value={name} maxLength={60} onChange={(e) => setName(e.target.value)} />
        <label className="consent">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          This is my voice (and face), or I have permission to clone them.
        </label>
        <button className="primary" disabled={!canSubmit} onClick={submit}>{busy ? "Creating…" : "Create voice"}</button>
        {status && <p role={status.kind === "error" ? "alert" : "status"} className={`lab-${status.kind}`}>{status.text}</p>}
      </section>
    </div>
  );
}
