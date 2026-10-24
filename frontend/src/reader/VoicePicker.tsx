import { useRef, type ReactNode } from "react";
import type { Book, Cast, Face, Gender } from "../api";
import {
  PRESETS, VOICE_INFO, genderRows, narratorGender, radioKeyTarget, slotFace, slotVoice, withGender, withSlot,
} from "./castLogic";

export type Choice = { value: string; title: string; detail?: string; image?: string | null };

/**
 * A row of selectable cards with radio semantics: one tab stop, arrow keys / Home / End move and select, Space and
 * Enter select. `extra` renders beside an option (a preview button), outside the radio itself.
 */
export function ChoiceGroup({ label, options, value, onChange, extra, className = "" }: {
  label: string; options: Choice[]; value: string; onChange: (v: string) => void;
  extra?: (o: Choice) => ReactNode; className?: string;
}) {
  const refs = useRef<(HTMLDivElement | null)[]>([]);
  const selected = Math.max(0, options.findIndex((o) => o.value === value));
  const pick = (i: number) => { onChange(options[i].value); refs.current[i]?.focus(); };
  return (
    <div role="radiogroup" aria-label={label} className={`choices ${className}`}>
      {options.map((o, i) => {
        const checked = o.value === value;
        return (
          <div key={o.value} className={`choice${checked ? " is-checked" : ""}`}>
            <div role="radio" aria-checked={checked} tabIndex={i === selected ? 0 : -1} className="choice-pick"
                 ref={(el) => { refs.current[i] = el; }}
                 onClick={() => pick(i)}
                 onKeyDown={(e) => {
                   if (e.key === " " || e.key === "Enter") { e.preventDefault(); pick(i); return; }
                   const t = radioKeyTarget(e.key, i, options.length);
                   if (t !== null) { e.preventDefault(); pick(t); }
                 }}>
              {o.image !== undefined && (
                <span className="face choice-face" aria-hidden="true">{o.image ? <img src={o.image} alt="" /> : "–"}</span>
              )}
              <span className="choice-text">
                <span className="choice-title">{o.title}</span>
                {o.detail && <span className="choice-detail">{o.detail}</span>}
              </span>
            </div>
            {extra?.(o)}
          </div>
        );
      })}
    </div>
  );
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
const GENDER_LABEL: Record<Gender, string> = { male: "Male voice", female: "Female voice" };

/** Voice cards: the presets (of one gender when `gender` is set) and the visitor's cloned voices. */
export function voiceChoices(cloned: Record<string, string>, gender: Gender | null, current: string): Choice[] {
  const presets = PRESETS.filter((v) => !gender || VOICE_INFO[v]?.gender === gender || v === current);
  const out: Choice[] = presets.map((v) => ({ value: v, title: cap(v), detail: VOICE_INFO[v]?.blurb }));
  for (const [name, id] of Object.entries(cloned)) out.push({ value: id, title: name, detail: "Your cloned voice" });
  if (!out.some((o) => o.value === current)) out.unshift({ value: current, title: current, detail: "Book voice" });
  return out;
}

export function faceChoices(faces: Face[], current: string | null): Choice[] {
  const out: Choice[] = [{ value: "", title: "No face", image: null }];
  if (current && !faces.some((f) => f.id === current)) out.push({ value: current, title: "(removed face)", image: null });
  return [...out, ...faces.map((f) => ({ value: f.id, title: f.name, image: f.image }))];
}

type PreviewProps = { onPreview: (voice: string) => void; audioAvailable: boolean };

function PreviewButton({ voice, title, onPreview, audioAvailable }: PreviewProps & { voice: string; title: string }) {
  return (
    <button type="button" className="choice-preview" disabled={!audioAvailable} onClick={() => onPreview(voice)}
            aria-label={`Preview ${title}`} title={`Hear ${title}`}>▶</button>
  );
}

/** One voice + face pair: the male or female voice of a novel, or a textbook's single voice. */
export function VoiceCard({ heading, voice, face, cloned, faces, gender, onVoice, onFace, note, ...preview }: PreviewProps & {
  heading: string; voice: string; face: string | null; cloned: Record<string, string>; faces: Face[];
  gender: Gender | null; onVoice: (v: string) => void; onFace: (f: string | null) => void; note?: string;
}) {
  return (
    <section className="voice-card" aria-label={heading}>
      <h4>{heading}</h4>
      {note && <p className="empty-note">{note}</p>}
      <ChoiceGroup label={`${heading}: voice`} options={voiceChoices(cloned, gender, voice)} value={voice} onChange={onVoice}
                   extra={(o) => <PreviewButton voice={o.value} title={o.title} {...preview} />} />
      <ChoiceGroup label={`${heading}: face`} className="choices-faces" options={faceChoices(faces, face)} value={face ?? ""}
                   onChange={(f) => onFace(f || null)} />
    </section>
  );
}

/** Novels in two-voice mode: the male and the female voice, who narrates, and per-character gender overrides. */
export function TwoVoices({ book, cast, onCast, cloned, faces, ...preview }: PreviewProps & {
  book: Book; cast: Cast; onCast: (c: Cast) => void; cloned: Record<string, string>; faces: Face[];
}) {
  const narrator = narratorGender(cast);
  const rows = genderRows(book, cast);
  return (
    <>
      <p className="empty-note">Every character reads in the male or the female voice, picked from what the book says about them.</p>
      {(["male", "female"] as Gender[]).map((g) => (
        <VoiceCard key={g} heading={GENDER_LABEL[g]} gender={g} voice={slotVoice(cast, g)} face={slotFace(cast, g)}
                   cloned={cloned} faces={faces} {...preview}
                   note={g === narrator ? "Also the narrator." : undefined}
                   onVoice={(v) => onCast(withSlot(cast, g, { voice: v }))}
                   onFace={(f) => onCast(withSlot(cast, g, { face: f }))} />
      ))}
      <div className="narrator-pick">
        <span className="narrator-pick-label">Narrator</span>
        <ChoiceGroup label="Narrator" className="choices-segmented" value={narrator}
                     options={[{ value: "male", title: "Male voice" }, { value: "female", title: "Female voice" }]}
                     onChange={(g) => onCast({ ...cast, narrator_gender: g as Gender })} />
      </div>
      {rows.length > 0 && (
        <details className="gender-list">
          <summary>Who reads in which voice ({rows.length} characters)</summary>
          <ul>
            {rows.map((r) => (
              <li key={r.id}>
                <span className="gender-name">{r.name}</span>
                <ChoiceGroup label={`${r.name} reads in`} className="choices-segmented" value={r.override ?? "auto"}
                             options={[
                               { value: "auto", title: r.auto ? `Auto (${r.auto})` : "Auto (narrator's voice)" },
                               { value: "male", title: "Male" }, { value: "female", title: "Female" }]}
                             onChange={(v) => onCast(withGender(cast, r.id, v === "auto" ? null : v as Gender))} />
              </li>
            ))}
          </ul>
        </details>
      )}
    </>
  );
}
