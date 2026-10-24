import { useEffect, useRef, useState } from "react";
import { cloneVoice, fetchVoices, type Book } from "../api";

export const PRESETS = ["chloe", "eleanor", "jake", "marcus", "nora", "oliver"];
const SCRIPT =
  "Well, it is a rather long story, but I have to tell you that I was walking home late one evening " +
  "when I noticed a light burning in the window of the old house.";

type Props = {
  book: Book;
  overrides: Record<string, string>;
  onOverride: (speaker: string, voice: string | null) => void;
  onPreview: (speaker: string, voice: string) => void;
  audioAvailable: boolean;
  onClose: () => void;
};

/** Cast panel (a voice per speaker) and Voice lab (clone your own voice, with consent). */
export default function VoiceLab({ book, overrides, onOverride, onPreview, audioAvailable, onClose }: Props) {
  const [cloned, setCloned] = useState<Record<string, string>>({});
  const [transcript, setTranscript] = useState(SCRIPT);
  const [name, setName] = useState("");
  const [consent, setConsent] = useState(false);
  const [clip, setClip] = useState<{ blob: Blob; filename: string } | null>(null);
  const [recording, setRecording] = useState(false);
  const [status, setStatus] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const recorder = useRef<MediaRecorder | null>(null);

  useEffect(() => { fetchVoices().then(setCloned).catch(() => setCloned({})); }, []);

  const speakers = [{ id: "narrator", name: "Narrator", voice: book.narrator?.voice ?? "marcus" },
    ...book.characters.map((c) => ({ id: c.id, name: c.name, voice: c.voice }))];

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
    } catch {
      setStatus({ kind: "error", text: "The microphone is not available. Allow access in the browser, or upload a file instead." });
    }
  }

  async function submit() {
    if (!clip) return;
    setBusy(true);
    setStatus(null);
    try {
      const out = await cloneVoice(clip.blob, clip.filename, transcript, name.trim(), consent);
      setCloned((c) => ({ ...c, [out.name]: out.voice }));
      setStatus({ kind: "ok", text: `Created "${out.name}". Pick it for any speaker above.` });
    } catch (e) {
      setStatus({ kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }

  const options = [...PRESETS.map((v) => ({ value: v, label: v })), ...Object.entries(cloned).map(([n, v]) => ({ value: v, label: `${n} (your voice)` }))];
  const canSubmit = !!clip && consent && name.trim().length > 0 && transcript.trim().length > 0 && !busy && audioAvailable;

  return (
    <div className="lab" role="dialog" aria-label="Voices">
      <div className="lab-head">
        <h2>Voices</h2>
        <button onClick={onClose}>Close</button>
      </div>

      <section>
        <h3>Cast</h3>
        <ul className="lab-cast">
          {speakers.map((s) => {
            const value = overrides[s.id] ?? s.voice;
            return (
              <li key={s.id}>
                <label htmlFor={`voice-${s.id}`}>{s.name}</label>
                <select id={`voice-${s.id}`} value={value} onChange={(e) => onOverride(s.id, e.target.value === s.voice ? null : e.target.value)}>
                  {!options.some((o) => o.value === value) && <option value={value}>{value}</option>}
                  {options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
                <button disabled={!audioAvailable} onClick={() => onPreview(s.id, value)}>Preview</button>
              </li>
            );
          })}
        </ul>
        {!audioAvailable && <p className="empty-note">Previews and cloning need the Boson API key on the server.</p>}
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
        <label htmlFor="lab-name">Name this voice</label>
        <input id="lab-name" value={name} maxLength={60} onChange={(e) => setName(e.target.value)} />
        <label className="consent">
          <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
          This is my voice, or I have permission to clone it.
        </label>
        <button className="primary" disabled={!canSubmit} onClick={submit}>{busy ? "Creating…" : "Create voice"}</button>
        {status && <p role={status.kind === "error" ? "alert" : "status"} className={`lab-${status.kind}`}>{status.text}</p>}
      </section>
    </div>
  );
}
