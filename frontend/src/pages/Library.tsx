import { useEffect, useRef, useState, type FormEvent } from "react";
import { fetchBooks, MAX_UPLOAD_BYTES, pollBookStatus, uploadBook, type BookKind, type BookSummary } from "../api";

type UploadState =
  | { phase: "idle" }
  | { phase: "uploading" | "processing"; text: string }
  | { phase: "ready"; title: string }
  | { phase: "error"; message: string };

function AddBook({ onReady }: { onReady: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [author, setAuthor] = useState("");
  const [kind, setKind] = useState<BookKind>("novel");
  const [state, setState] = useState<UploadState>({ phase: "idle" });
  const abortRef = useRef<AbortController | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => () => abortRef.current?.abort(), []); // stop polling on unmount

  const busy = state.phase === "uploading" || state.phase === "processing";

  function pick(f: File | null) {
    setFile(f);
    if (f) setTitle(f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim().slice(0, 120));
    setState({ phase: "idle" });
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (busy) return;
    if (!file) return setState({ phase: "error", message: "Choose a PDF or .txt file first." });
    const isPdf = /\.pdf$/i.test(file.name);
    if (!isPdf && !/\.txt$/i.test(file.name)) return setState({ phase: "error", message: "Only PDF and .txt files can be added." });
    if (file.size > MAX_UPLOAD_BYTES) return setState({ phase: "error", message: "That file is over 20 MB. Choose a smaller one." });
    const name = title.trim();
    if (!name) return setState({ phase: "error", message: "Give the book a title." });

    const ctl = new AbortController();
    abortRef.current?.abort();
    abortRef.current = ctl;
    setState({ phase: "uploading", text: "Uploading…" });
    try {
      const { book_id } = await uploadBook(file, name, author.trim(), kind);
      if (ctl.signal.aborted) return;
      setState({ phase: "processing", text: isPdf ? "Reading the PDF…" : "Reading the text…" });
      const result = await pollBookStatus(book_id, ctl.signal);
      if (!result || ctl.signal.aborted) return;
      if (result.status === "ready") {
        setState({ phase: "ready", title: name });
        setFile(null); setTitle(""); setAuthor("");
        if (fileRef.current) fileRef.current.value = "";
        onReady();
      } else {
        setState({ phase: "error", message: result.message || "The book could not be processed." });
      }
    } catch (err) {
      if (!ctl.signal.aborted) setState({ phase: "error", message: (err as Error).message });
    }
  }

  return (
    <section className="add-book" aria-labelledby="add-book-h">
      <h2 id="add-book-h">Add a book</h2>
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="ab-file">PDF or text file (up to 20 MB)</label>
          <input id="ab-file" ref={fileRef} type="file" accept=".pdf,.txt" disabled={busy}
            onChange={(e) => pick(e.target.files?.[0] ?? null)} />
        </div>
        <div className="field">
          <label htmlFor="ab-title">Title</label>
          <input id="ab-title" type="text" value={title} maxLength={120} required disabled={busy}
            onChange={(e) => setTitle(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="ab-author">Author (optional)</label>
          <input id="ab-author" type="text" value={author} maxLength={120} disabled={busy}
            onChange={(e) => setAuthor(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="ab-kind">Kind</label>
          <select id="ab-kind" value={kind} disabled={busy} onChange={(e) => setKind(e.target.value as BookKind)}>
            <option value="novel">Novel</option>
            <option value="textbook">Textbook</option>
          </select>
        </div>
        <button type="submit" className="add-book-go" disabled={busy}>Add a book</button>
      </form>
      <div className="add-book-status" role="status">
        {(state.phase === "uploading" || state.phase === "processing") && state.text}
        {state.phase === "ready" && `Ready: ${state.title}`}
      </div>
      {state.phase === "error" && <p className="add-book-error" role="alert">{state.message}</p>}
      <p className="add-book-note">Uploads are kept on this machine and are not shared.</p>
    </section>
  );
}

export default function Library() {
  const [books, setBooks] = useState<BookSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = () => fetchBooks().then(setBooks).catch((e: Error) => setError(e.message));
  useEffect(() => { load(); }, []);

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

      <AddBook onReady={load} />

      <p className="licence">
        Fiction is public domain from Project Gutenberg. The Biology chapter comes from{" "}
        <a href="https://openstax.org/details/books/biology-2e">Biology 2e by OpenStax</a>, licensed CC BY 4.0.
      </p>
    </main>
  );
}
