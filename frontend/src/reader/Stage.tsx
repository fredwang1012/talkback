import { useState } from "react";
import type { Book, Health } from "../api";
import { splitCharacters } from "./position";

type Props = {
  book: Book;
  para: number;
  speakerId: string;
  onSpeaker: (id: string) => void;
  health: Health | null;
};

export default function Stage({ book, para, speakerId, onSpeaker, health }: Props) {
  const [mode] = useState<"read" | "talk">("read");
  const { met, locked } = splitCharacters(book.characters, para);
  const speaker = met.find((c) => c.id === speakerId);
  const name = speaker?.name ?? "Narrator";
  const blurb = speaker ? speaker.persona : book.narrator?.is_character
    ? `Tells the story as ${book.characters.find((c) => c.id === book.narrator?.is_character)?.name ?? "a character"}.`
    : "Reads the text as written.";

  return (
    <aside className="stage" aria-label="Stage">
      <div className="mode" role="group" aria-label="Mode">
        <button aria-pressed={mode === "read"}>Read</button>
        <button aria-pressed={false} disabled title="Talk mode is not built yet">Talk</button>
      </div>

      <div className="speaker">
        <div className="medallion" aria-hidden="true">{name.charAt(0)}</div>
        <div>
          <h3>{name}</h3>
          <p>{blurb}</p>
        </div>
      </div>

      <div className="badge">
        <strong>{speaker ? `${name} knows` : "Story position:"}</strong> up to paragraph {para + 1}.
        {locked.length > 0 && <> {locked.length} more {locked.length === 1 ? "character appears" : "characters appear"} later.</>}
      </div>

      <section aria-label="Who to talk to">
        <ul className="cast-list">
          <li>
            <button aria-pressed={speakerId === "narrator"} onClick={() => onSpeaker("narrator")}>
              Narrator <small>{book.narrator?.voice ?? "voice not set"}</small>
            </button>
          </li>
          {met.map((c) => (
            <li key={c.id}>
              <button aria-pressed={speakerId === c.id} onClick={() => onSpeaker(c.id)}>
                {c.name} <small>{c.voice}</small>
              </button>
            </li>
          ))}
          {locked.map((c) => (
            <li key={c.id}>
              <button disabled title="Not met yet">
                {c.name} <small>not met yet</small>
              </button>
            </li>
          ))}
        </ul>
        {book.characters.length === 0 && (
          <p className="empty-note">No cast yet. Characters appear here once the casting step has run.</p>
        )}
      </section>

      <div className="mic">
        <button className="mic-button" disabled aria-label="Hold to talk (not available yet)">
          <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
            <rect x="9" y="3" width="6" height="12" rx="3" /><path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
          </svg>
        </button>
        <p>
          {health?.boson_key_set
            ? "Voice questions are not built yet."
            : "Voice questions need the Boson API key and are not built yet."}
        </p>
      </div>
      <div className="captions" aria-live="polite">Captions of the answer will appear here.</div>
    </aside>
  );
}
