import RecapMenu, { type RecapChoice } from "./RecapMenu";
import { BILINGUAL_LANGUAGES, pickLanguage, type BiMode, type BiSettings } from "./bilingual";

type Props = {
  playing: boolean; onToggle: () => void; onPrev: () => void; onNext: () => void;
  speed: number; speeds: number[]; onSpeed: (s: number) => void;
  naturalSpeed?: boolean; onNaturalSpeed?: (v: boolean) => void; // #78: re-voice with Boson speed tags (hidden when absent)
  onVoices: () => void;
  autoResume: boolean; onAutoResume: (v: boolean) => void;
  hasFigures?: boolean; readCaptions?: boolean; onReadCaptions?: (v: boolean) => void; // textbook figures (#40)
  para: number; totalParas: number; atStart: boolean; atEnd: boolean; hasCast: boolean;
  onRecap: (c: RecapChoice) => void;
  inSeries: boolean;
  textbook?: boolean;
  loading?: boolean; audioAvailable: boolean; audioError: string | null; audioNotice?: string | null;
  bilingual?: { value: BiSettings; onChange: (s: BiSettings) => void }; // B3: second language, remembered per book
};

const Icon = ({ d }: { d: string }) => (
  <svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d={d} /></svg>
);

export default function PlayerBar(p: Props) {
  return (
    <footer className="player" aria-label="Player">
      <div className="transport">
        <button className="icon-btn" onClick={p.onPrev} disabled={p.atStart} aria-label="Previous sentence (left arrow)">
          <Icon d="M6 5h2v14H6zM20 5v14L9.5 12z" />
        </button>
        <button className="icon-btn primary" onClick={p.onToggle} disabled={p.atEnd && !p.playing} aria-label={p.playing ? "Pause (k)" : p.audioAvailable ? "Play (k)" : "Play silently (k)"} aria-busy={p.loading}>
          <Icon d={p.playing ? "M6 5h4v14H6zM14 5h4v14h-4z" : "M8 5v14l11-7z"} />
        </button>
        <button className="icon-btn" onClick={p.onNext} disabled={p.atEnd} aria-label="Next sentence (right arrow)">
          <Icon d="M16 5h2v14h-2zM4 5l10.5 7L4 19z" />
        </button>
      </div>
      <label>
        Speed
        <select value={p.speed} onChange={(e) => p.onSpeed(Number(e.target.value))}>
          {p.speeds.map((s) => <option key={s} value={s}>{s}x</option>)}
        </select>
      </label>
      {p.onNaturalSpeed && p.audioAvailable && (
        <label title="Natural speed re-voices the audio at 0.75x, 1.25x and 1.5x. It uses Boson credit.">
          <input type="checkbox" checked={!!p.naturalSpeed} onChange={(e) => p.onNaturalSpeed?.(e.target.checked)} />
          Natural speed
        </label>
      )}
      <button className="recap-btn" onClick={p.onVoices}>Character Control</button>
      <label>
        <input type="checkbox" checked={p.autoResume} onChange={(e) => p.onAutoResume(e.target.checked)} />
        Auto-resume
      </label>
      {p.hasFigures && (
        <label>
          <input type="checkbox" checked={p.readCaptions ?? true} onChange={(e) => p.onReadCaptions?.(e.target.checked)} />
          Read captions
        </label>
      )}
      {p.bilingual && (
        <span className="bilingual">
          <label title="Read each paragraph again in another language, in the same voice. Translation uses Gemini; new audio uses Boson credit.">
            Second language
            <select value={p.bilingual.value.mode === "off" ? "" : p.bilingual.value.lang}
                    onChange={(e) => p.bilingual?.onChange(pickLanguage(p.bilingual.value, e.target.value))}>
              <option value="">Off</option>
              {BILINGUAL_LANGUAGES.map((l) => <option key={l.code} value={l.code}>{l.label}</option>)}
            </select>
          </label>
          {p.bilingual.value.mode !== "off" && (
            <select aria-label="Bilingual mode" value={p.bilingual.value.mode}
                    onChange={(e) => p.bilingual?.onChange({ ...p.bilingual.value, mode: e.target.value as BiMode })}>
              <option value="bilingual">Original, then translation</option>
              <option value="translated">Translation only</option>
            </select>
          )}
        </span>
      )}
      <RecapMenu className="recap-btn" inSeries={p.inSeries} textbook={p.textbook} onPick={p.onRecap} />
      <span className="pos">¶ {p.para + 1} / {p.totalParas}</span>
      <p className="note" role={p.audioError ? "alert" : undefined}>
        {p.audioError
          ? `Audio problem: ${p.audioError}`
          : p.audioNotice
            ? p.audioNotice
            : p.audioAvailable
            ? p.loading ? "Preparing audio…"
              : p.naturalSpeed ? "Natural speed re-voices audio at 0.75x, 1.25x and 1.5x (uses Boson credit)."
              : "Reading aloud. Click a sentence to jump."
            : "No audio yet: play moves through the text at an estimated reading pace. Voices need the Boson API key."}
      </p>
    </footer>
  );
}
