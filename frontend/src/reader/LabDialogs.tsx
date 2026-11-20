import { useEffect, useRef, useState } from "react";
import { cloneVoice, fetchAccountVoices, importVoice, previewVoice, uploadFace, type AccountVoice, type Face } from "../api";
import { micErrorMessage } from "../audio/mediaErrors";
import { toCloneAudio } from "../audio/toWav";
import CameraCapture from "./CameraCapture";
import FaceCropper from "./FaceCropper";
import { faceMissing, missingLine, voiceMissing } from "./labForms";

const VOICE_HELP = "10–30 seconds of clear speech in a quiet room (at least 3 s, up to 10 MB). Type exactly what you said.";
const FACE_HELP = "PNG, JPEG or WebP up to 5 MB. One face looking at the camera, head and shoulders, even light.";
const SCRIPT =
  "Well, it is a rather long story, but I have to tell you that I was walking home late one evening " +
  "when I noticed a light burning in the window of the old house.";

type Msg = { kind: "ok" | "error"; text: string } | null;
const errText = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** Add face dialog body (#111): name, photo (file or camera) -> crop -> Use, optional Animate, Upload. */
export function AddFaceForm({ onDone, onCancel }: { onDone: (face: Face, animate: boolean) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [raw, setRaw] = useState<File | null>(null);     // picked or taken, waiting for its crop (#109)
  const [file, setFile] = useState<File | null>(null);   // the cropped portrait
  const [preview, setPreview] = useState<string | null>(null);
  const [animate, setAnimate] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  useEffect(() => {
    if (!file) { setPreview(null); return; }
    const u = URL.createObjectURL(file);
    setPreview(u);
    return () => URL.revokeObjectURL(u);
  }, [file]);

  const missing = missingLine(faceMissing({ file: !!file, cropping: !!raw, name, busy }));
  async function submit() {
    if (!file || missing || busy) return;
    setBusy(true);
    setMsg(null);
    try {
      onDone(await uploadFace(file, name.trim()), animate);
    } catch (e) {
      setMsg({ kind: "error", text: errText(e) });
      setBusy(false);
    }
  }
  const choose = (f: File | null) => { if (f) { setRaw(f); setFile(null); } };

  return (
    <>
      <div className="add-book-head">
        <h2 id="add-face-h">Add face</h2>
        <button type="button" onClick={onCancel}>Close</button>
      </div>
      <p className="empty-note">{FACE_HELP} Animating gives it a talking and an idle loop; otherwise it shows as a still picture.</p>
      <label htmlFor="face-name">Face name</label>
      <input id="face-name" value={name} maxLength={60} autoFocus onChange={(e) => setName(e.target.value)} />
      <div className="lab-row">
        <label className="file" htmlFor="face-file">
          Photo
          <input id="face-file" type="file" accept="image/png,image/jpeg,image/webp"
                 onChange={(e) => { choose(e.target.files?.[0] ?? null); e.target.value = ""; }} />
        </label>
        <CameraCapture onUse={choose} />
      </div>
      {raw && <FaceCropper file={raw} onUse={(f) => { setFile(f); setRaw(null); }} onCancel={() => setRaw(null)} />}
      {file && preview && (
        <div className="lab-row">
          <span className="face"><img src={preview} alt="Cropped portrait" /></span>
          <span className="empty-note">Portrait ready.</span>
        </div>
      )}
      <label className="consent">
        <input type="checkbox" checked={animate} onChange={(e) => setAnimate(e.target.checked)} />
        Animate after upload (1 to 2 min)
      </label>
      <div className="lab-row">
        <button type="button" className="primary" disabled={!!missing || busy} onClick={() => void submit()}>{busy ? "Uploading…" : "Upload"}</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
      {missing && <p className="lab-missing" aria-live="polite">{missing}</p>}
      {msg && <p role="alert" className="lab-error">{msg.text}</p>}
    </>
  );
}

export type ClonedVoice = { name: string; voice: string; face: string | null };

/** Add voice dialog body (#111): the line to read, Record or upload, transcript, name, an existing face or none,
 * Create voice (a one-line permission reminder, no checkbox). The chosen face is linked by id (#113): no copy, so the face list shows it once. */
export function AddVoiceForm({ faces, audioAvailable, onDone, onCancel }: {
  faces: Face[]; audioAvailable: boolean; onDone: (out: ClonedVoice) => void; onCancel: () => void;
}) {
  const [transcript, setTranscript] = useState(SCRIPT);
  const [name, setName] = useState("");
  const [clip, setClip] = useState<{ blob: Blob; filename: string } | null>(null);
  const [faceId, setFaceId] = useState("");
  const [recording, setRecording] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Msg>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  useEffect(() => () => { if (recorder.current?.state === "recording") recorder.current.stop(); }, []); // closed mid-take
  const [previewing, setPreviewing] = useState(false);
  const previewAudio = useRef<HTMLAudioElement | null>(null);
  useEffect(() => () => { previewAudio.current?.pause(); }, []);
  // voices already on the Boson account (owner or local run only: null = not shown)
  const [account, setAccount] = useState<AccountVoice[] | null>(null);
  useEffect(() => {
    let live = true;
    fetchAccountVoices().then((v) => { if (live) setAccount(v); }).catch(() => { if (live) setAccount(null); });
    return () => { live = false; };
  }, []);

  async function preview() {
    if (!clip || !transcript.trim() || previewing) return;
    setPreviewing(true);
    setMsg(null);
    try {
      const audio = await toCloneAudio(clip.blob, clip.filename);
      const mp3 = await previewVoice(audio.blob, audio.filename, transcript);
      previewAudio.current?.pause();
      const el = new Audio(URL.createObjectURL(mp3));
      el.onended = () => URL.revokeObjectURL(el.src);
      previewAudio.current = el;
      await el.play();
    } catch (e) {
      setMsg({ kind: "error", text: errText(e) });
    } finally {
      setPreviewing(false);
    }
  }

  async function importOne(v: AccountVoice) {
    if (busy) return;
    setBusy(true);
    setMsg(null);
    try {
      onDone(await importVoice(v.id, (name.trim() || v.title).slice(0, 60)));
    } catch (e) {
      setMsg({ kind: "error", text: errText(e) });
      setBusy(false);
    }
  }

  async function startRecording() {
    setMsg(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const rec = new MediaRecorder(stream);
      const parts: Blob[] = [];
      rec.ondataavailable = (e) => parts.push(e.data);
      rec.onstop = () => {
        stream.getTracks().forEach((t) => t.stop());
        const type = rec.mimeType || "audio/webm";
        setClip({ blob: new Blob(parts, { type }), filename: `recording.${type.includes("ogg") ? "ogg" : "webm"}` });
        setRecording(false);
      };
      recorder.current = rec;
      rec.start();
      setRecording(true);
    } catch (e) {
      setMsg({ kind: "error", text: `${micErrorMessage(e)} Or upload a file instead.` });
    }
  }

  const items = voiceMissing({ clip: !!clip, transcript, name, audioAvailable, busy });
  if (recording) items.unshift("the recording stopped");
  const missing = missingLine(items);
  async function submit() {
    if (!clip || missing || busy) return;
    setBusy(true);
    setMsg(null);
    try {
      const chosen = faces.find((f) => f.id === faceId);
      const audio = await toCloneAudio(clip.blob, clip.filename);
      onDone(await cloneVoice(audio.blob, audio.filename, transcript, name.trim(), true, null, chosen?.id ?? null));
    } catch (e) {
      setMsg({ kind: "error", text: errText(e) });
      setBusy(false);
    }
  }

  return (
    <>
      <div className="add-book-head">
        <h2 id="add-voice-h">Add voice</h2>
        <button type="button" onClick={onCancel}>Close</button>
      </div>
      <p className="empty-note">Read the line below aloud, then check that the text matches what you said. {VOICE_HELP}</p>
      <label htmlFor="lab-transcript">What you will say</label>
      <textarea id="lab-transcript" rows={3} value={transcript} onChange={(e) => setTranscript(e.target.value)} />
      <div className="lab-row">
        <button type="button" onClick={recording ? () => recorder.current?.stop() : () => void startRecording()}>
          {recording ? "Stop recording" : "Record"}
        </button>
        <label className="file">
          Or upload a file
          <input type="file" accept="audio/*"
                 onChange={(e) => { const f = e.target.files?.[0]; if (f) setClip({ blob: f, filename: f.name }); e.target.value = ""; }} />
        </label>
      </div>
      {clip && (
        <div className="lab-row">
          <p className="empty-note">Ready: {clip.filename} ({Math.round(clip.blob.size / 1024)} KB)</p>
          <button type="button" disabled={previewing || !transcript.trim()} onClick={() => void preview()}>
            {previewing ? "Making a sample…" : "Hear it first"}
          </button>
        </div>
      )}
      <label htmlFor="lab-name">Name this voice</label>
      <input id="lab-name" value={name} maxLength={60} onChange={(e) => setName(e.target.value)} />
      <label htmlFor="lab-voice-face">Face shown when this voice speaks</label>
      <select id="lab-voice-face" value={faceId} onChange={(e) => setFaceId(e.target.value)}>
        <option value="">None</option>
        {faces.map((f) => <option key={f.id} value={f.id}>{f.name}</option>)}
      </select>
      {faces.length === 0 && <p className="empty-note">No faces yet: add one with Add face, then pick it here.</p>}
      <p className="empty-note">Only add a voice you have permission to use.</p>
      <div className="lab-row">
        <button type="button" className="primary" disabled={!!missing || busy} onClick={() => void submit()}>{busy ? "Creating…" : "Create voice"}</button>
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
      {missing && <p className="lab-missing" aria-live="polite">{missing}</p>}
      {msg && <p role="alert" className="lab-error">{msg.text}</p>}
      {account && account.length > 0 && (
        <>
          <h3 className="lab-subhead">Already on your Boson account</h3>
          <p className="empty-note">Voices made earlier with this key. Add one without recording again{name.trim() ? `, named "${name.trim()}"` : ""}.</p>
          <ul className="lab-account-voices">
            {account.map((v) => (
              <li key={v.id}>
                <span>{v.title}</span>
                <button type="button" disabled={busy} onClick={() => void importOne(v)}>Add</button>
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}
