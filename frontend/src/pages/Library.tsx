import { useEffect, useState } from "react";
import { fetchBooks, type BookSummary } from "../api";

export default function Library() {
  const [books, setBooks] = useState<BookSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetchBooks().then(setBooks).catch((e: Error) => setError(e.message));
  }, []);

  const hasTextbook = books?.some((b) => b.kind === "textbook") ?? false;

  return (
    <main className="library">
      <header className="masthead">
        <h1>Talkback</h1>
        <p>Books read aloud in full cast. Stop anywhere, ask a question out loud, and the story picks up from the same sentence.</p>
      </header>

      {error && (
        <p className="state-line" role="alert">
          The library could not be reached ({error}). Start the backend with <code>uv run uvicorn app.main:app --port 8000</code>.
        </p>
      )}
      {!books && !error && <p className="state-line">Opening the shelves…</p>}

      {books && (
        <section className="shelf" aria-label="Books">
          {books.map((b) => (
            <a key={b.id} className="volume" href={`#/read/${b.id}`}>
              {b.cover && <img className="cover" src={b.cover} alt="" />}
              <div>
                <h2>{b.title}</h2>
                <p className="by">{b.author}</p>
              </div>
              <div>
                <ul className="facts">
                  <li>{b.sections} parts, {b.paragraphs} paragraphs</li>
                  <li>{b.characters > 0 ? `${b.characters} characters you can talk to` : "Narrator only so far; the cast is not set yet"}</li>
                </ul>
                <div className="open">Open the book</div>
              </div>
            </a>
          ))}
          {!hasTextbook && (
            <div className="volume volume-closed" aria-disabled="true">
              <div>
                <h2>Biology 2e, chapter 8</h2>
                <p className="by">OpenStax</p>
              </div>
              <div>
                <ul className="facts">
                  <li>Photosynthesis, explained by a narrator you can interrupt.</li>
                </ul>
                <div className="open">Needs the chapter text in data/raw</div>
              </div>
            </div>
          )}
        </section>
      )}

      <p className="licence">
        Fiction is public domain from Project Gutenberg. The Biology chapter comes from{" "}
        <a href="https://openstax.org/details/books/biology-2e">Biology 2e by OpenStax</a>, licensed CC BY 4.0.
      </p>
    </main>
  );
}
