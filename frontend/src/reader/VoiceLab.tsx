import { useRef, useState, type Dispatch, type SetStateAction } from "react";
import { animateFace, deleteFace, deleteVoice, type Book, type Cast, type Face } from "../api";
import Modal from "../ui/Modal";
import { PRESETS, castFace, castVoice, isNovel, runAnimate, withAssignment } from "./castLogic";
import { AddFaceForm, AddVoiceForm, type ClonedVoice } from "./LabDialogs";
import { NovelVoices, VoiceCard } from "./VoicePicker";

export { PRESETS };

type Props = {
  book: Book;
  cast: Cast;
  onCast: (cast: Cast) => void; // a changed cast; the parent saves it
  castError: string | null;
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
  onPreview: (voice: string) => void; // speaks a short fixed line in that voice (cached after the first time)
  audioAvailable: boolean;
  onClose: () => void;
};

/** Voices panel (one or two voices for a novel, one for a textbook) and Voice lab (clone your own voice). */
export default function VoiceLab({ book, cast, onCast, castError, cloned, onCloned, animating, setAnimating, animErrors, setAnimErrors, faces, onFaces, onFacesChanged, onRemoved, onPreview, audioAvailable, onClose }: Props) {
  const [adding, setAdding] = useState<"voice" | "face" | null>(null); // which Add dialog is open (#111)
  const addVoiceBtn = useRef<HTMLButtonElement>(null);
  const addFaceBtn = useRef<HTMLButtonElement>(null);
  const [labMsg, setLabMsg] = useState<string | null>(null);
  function closeAdd() {
    const which = adding;
    if (!which) return; // the dialog's own close event after we closed it
    setAdding(null);
    (which === "voice" ? addVoiceBtn : addFaceBtn).current?.focus();
  }
  function voiceAdded(out: ClonedVoice) {
    onCloned((c) => ({ ...c, [out.name]: out.voice }));
    onFacesChanged(); // reloads voices and faces (a linked face copy may have been made)
    setLabMsg(`Created "${out.name}". Pick it for any speaker in the voice lists above.`);
    closeAdd();
  }
  function faceAdded(f: Face, animateNow: boolean) {
    onFaces((list) => [...list, f]);
    setLabMsg(`Saved "${f.name}". Pick it from any face list.`);
    closeAdd();
    if (animateNow) void animate(f.id);
  }

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

  return (
    <div className="lab" role="dialog" aria-label="Voices">
      <div className="lab-head">
        <h2>Voices</h2>
        <button onClick={onClose}>Close</button>
      </div>

      <section aria-label="Voices and faces for reading">
        <h3>{isNovel(book) ? "Voices for this book" : "Voice"}</h3>
        {isNovel(book) ? (
          <NovelVoices book={book} cast={cast} onCast={onCast} cloned={cloned} faces={faces}
                       onPreview={onPreview} audioAvailable={audioAvailable} />
        ) : (
          <VoiceCard heading="Narrator" gender={null} voice={castVoice(book, cast, "narrator")} face={castFace(book, cast, "narrator")}
                     cloned={cloned} faces={faces} onPreview={onPreview} audioAvailable={audioAvailable}
                     onVoice={(v) => onCast(withAssignment(cast, "narrator", { voice: v }))}
                     onFace={(f) => onCast(withAssignment(cast, "narrator", { face: f }))} />
        )}
        {castError && <p role="alert" className="lab-error">{castError}</p>}
        {!audioAvailable && <p className="empty-note">Previews and cloning need the Boson API key on the server.</p>}
      </section>

      <section aria-label="Faces">
        <h3>Faces</h3>
        {faces.length === 0 && <p className="empty-note">No faces yet.</p>}
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

      <section aria-label="Add">
        <div className="lab-add">
          <button ref={addVoiceBtn} className="primary" aria-haspopup="dialog" onClick={() => { setLabMsg(null); setAdding("voice"); }}>Add voice</button>
          <button ref={addFaceBtn} className="primary" aria-haspopup="dialog" onClick={() => { setLabMsg(null); setAdding("face"); }}>Add face</button>
        </div>
        {labMsg && <p role="status" className="lab-ok">{labMsg}</p>}
      </section>

      <Modal open={adding === "voice"} onClose={closeAdd} labelledBy="add-voice-h">
        <AddVoiceForm faces={faces} audioAvailable={audioAvailable} onDone={voiceAdded} onCancel={closeAdd} />
      </Modal>
      <Modal open={adding === "face"} onClose={closeAdd} labelledBy="add-face-h">
        <AddFaceForm onDone={faceAdded} onCancel={closeAdd} />
      </Modal>
    </div>
  );
}
