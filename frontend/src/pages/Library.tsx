import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  accountLabel, changeKind, fetchBooks, fetchMe, SIGN_OUT_URL, type Me, MAX_UPLOAD_BYTES, pollBookStatus, pollEnrichment, pollIndexing, indexingLabel, startEnrichment, uploadBook,
  type BookStatus, type BookSummary, type UploadKind,
  enrichDoneNote,
} from "../api";
import { clearAccessToken, getAccessToken } from "../accessToken";
import { canMove, groupShelves, kindLabel, moveLabel, moveTarget, seriesTitle } from "../shelves";

type UploadState =
  | { phase: "idle" }
  | { phase: "uploading" | "processing"; text: string }
  | { phase: "enriching"; title: string; message: string; progress: number; detected?: string }
  | { phase: "confirm"; bookId: string; title: string; estimate: number }
  | { phase: "ready"; title: string; note?: string; detected?: string }
  | { phase: "error"; message: string };

const pct = (p: number | undefined) => Math.round(Math.max(0, Math.min(1, p ?? 0)) * 100);

function AddBook({ onReady }: { onReady: () => void }) {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [author, setAuthor] = useState("");
  const [kind, setKind] = useState<UploadKind>("auto");
  const [state, setState] = useState<UploadState>({ phase: "idle" });
  const abortRef = useRef<AbortController | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => () => abortRef.current?.abort(), []); // stop polling on unmount

  const busy = state.phase === "uploading" || state.phase === "processing" || state.phase === "enriching";

  function pick(f: File | null) {
    setFile(f);
    if (f) setTitle(f.name.replace(/\.[^.]+$/, "").replace(/[_-]+/g, " ").trim().slice(0, 120));
    setState({ phase: "idle" });
  }

  /** Novels keep processing after they are readable: show the stage and progress, or the estimate to confirm. */
  async function follow(bookId: string, name: string, first: BookStatus, ctl: AbortController) {
    let st: BookStatus | null = first;
    if (st.index?.state === "running") {  // readable already; cloud search is still being built
      const showIdx = (s: BookStatus) => { if (s.index) setState({ phase: "processing", text: `${name} can be read now. ${indexingLabel(s.index)}` }); };
      showIdx(st);
      st = await pollIndexing(bookId, showIdx, ctl.signal);
      if (!st || ctl.signal.aborted) return;
    }
    if (st.enrich?.status === "running") {
      const show = (s: BookStatus) => setState({
        phase: "enriching", title: name, message: s.enrich?.message ?? "Adding characters", progress: s.enrich?.progress ?? 0,
        detected: first.kind_detected ? kindLabel(first.kind_detected.kind) : undefined,
      });
      show(st);
      st = await pollEnrichment(bookId, show, ctl.signal);
      if (!st || ctl.signal.aborted) return;
    }
    const e = st.enrich;
    const detected = first.kind_detected ? kindLabel(first.kind_detected.kind) : undefined;
    if (st.status === "error") setState({ phase: "error", message: st.message || "The book could not be processed." });
    else if (e?.status === "awaiting_confirmation") setState({ phase: "confirm", bookId, title: name, estimate: e.estimate_usd ?? 0 });
    else if (e?.status === "done") { setState({ phase: "ready", title: name, note: enrichDoneNote(e), detected }); onReady(); }
    else if (e?.status === "refused" || e?.status === "error") setState({ phase: "ready", title: name, note: e.message, detected });
    else setState({ phase: "ready", title: name, detected });
  }

  async function confirm() {
    if (state.phase !== "confirm") return;
    const { bookId, title: name } = state;
    const ctl = new AbortController();
    abortRef.current?.abort();
    abortRef.current = ctl;
    setState({ phase: "enriching", title: name, message: "Starting", progress: 0 });
    try {
      const st = await startEnrichment(bookId);
      if (!ctl.signal.aborted) await follow(bookId, name, st, ctl);
    } catch (err) {
      if (!ctl.signal.aborted) setState({ phase: "error", message: (err as Error).message });
    }
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
        setFile(null); setTitle(""); setAuthor("");
        if (fileRef.current) fileRef.current.value = "";
        onReady(); // the book is readable (narrator only) from here on
        await follow(book_id, name, result, ctl);
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
          <select id="ab-kind" value={kind} disabled={busy} onChange={(e) => setKind(e.target.value as UploadKind)}>
            <option value="auto">Auto-detect</option>
            <option value="novel">Novel</option>
            <option value="textbook">Textbook</option>
          </select>
        </div>
        <button type="submit" className="add-book-go" disabled={busy}>Add a book</button>
      </form>
      <div className="add-book-status" role="status">
        {(state.phase === "uploading" || state.phase === "processing") && state.text}
        {state.phase === "enriching" && (
          <>
            <span>{state.title} can be read now.{state.detected && ` Detected: ${state.detected}.`} {state.message} ({pct(state.progress)}%)</span>
            <progress max={100} value={pct(state.progress)} aria-label="Character processing progress" />
          </>
        )}
        {state.phase === "ready" && `Ready: ${state.title}${state.detected ? `. Detected: ${state.detected}` : ""}${state.note ? `. ${state.note}` : ""}`}
      </div>
      {state.phase === "confirm" && (
        <div className="add-book-confirm">
          <p>{state.title} can be read now with the narrator. Add the characters' voices and the recap?</p>
          <button type="button" className="add-book-go" onClick={confirm}>
            Process with characters (~${state.estimate.toFixed(2)})
          </button>
        </div>
      )}
      {state.phase === "error" && <p className="add-book-error" role="alert">{state.message}</p>}
      <p className="add-book-note">The file stays on this computer. With cloud search on, the book's text is sent to Google Cloud to make it searchable.</p>
    </section>
  );
}

type MoveNote = { id: string; text: string; progress?: number; confirm?: number };

function Volume({ b, busy, note, onMove, onConfirm }: {
  b: BookSummary; busy: boolean; note?: MoveNote; onMove: (b: BookSummary) => void; onConfirm: (b: BookSummary) => void;
}) {
  return (
    <div className="volume-wrap">
      <a className="volume" href={`#/read/${b.id}`}>
        {b.cover && <img className="cover" src={b.cover} alt="" />}
        <div>
          <h3>{b.title}</h3>
          <p className="by">{b.author}</p>
        </div>
        <div>
          <ul className="facts">
            <li>{b.sections} {b.sections === 1 ? "chapter" : "chapters"}, {b.paragraphs} paragraphs</li>
            {b.kind === "novel" && (
              <li>{b.characters > 0 ? `${b.characters} characters you can talk to` : "Narrator only so far; the cast is not set yet"}</li>
            )}
          </ul>
          <div className="open">Open the book</div>
        </div>
      </a>
      {canMove(b) && (
        <button type="button" className="volume-move" disabled={busy} onClick={() => onMove(b)}
          aria-label={`${moveLabel(b)}: ${b.title}`}>
          {moveLabel(b)}
        </button>
      )}
      {note && (
        <div className="volume-note" role="status">
          <span>{note.text}{note.progress !== undefined && ` (${pct(note.progress)}%)`}</span>
          {note.progress !== undefined && <progress max={100} value={pct(note.progress)} aria-label={`Processing ${b.title}`} />}
          {note.confirm !== undefined && (
            <button type="button" className="volume-move" onClick={() => onConfirm(b)}>
              Process with characters (~${note.confirm.toFixed(2)})
            </button>
          )}
        </div>
      )}
    </div>
  );
}

export default function Library() {
  const [books, setBooks] = useState<BookSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, MoveNote>>({});
  const [moving, setMoving] = useState(false);
  const [hasCode, setHasCode] = useState(() => getAccessToken() !== "");
  const [me, setMe] = useState<Me | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  const load = () => fetchBooks().then(setBooks).catch((e: Error) => setError(e.message));
  useEffect(() => { load(); fetchMe().then(setMe); }, []);
  useEffect(() => () => abortRef.current?.abort(), []);

  const setNote = (id: string, note: MoveNote | null) =>
    setNotes((n) => { const { [id]: _drop, ...rest } = n; return note ? { ...rest, [id]: note } : rest; });

  /** Show enrichment progress for a book that was just moved to Novels (or confirm its cost in testing mode). */
  async function follow(b: BookSummary, first: BookStatus, ctl: AbortController) {
    let st: BookStatus | null = first;
    if (st.enrich?.status === "running") {
      const show = (s: BookStatus) => setNote(b.id, { id: b.id, text: s.enrich?.message ?? "Adding characters", progress: s.enrich?.progress ?? 0 });
      show(st);
      st = await pollEnrichment(b.id, show, ctl.signal);
      if (!st || ctl.signal.aborted) return;
    }
    const e = st.enrich;
    if (e?.status === "awaiting_confirmation") setNote(b.id, { id: b.id, text: "Characters, voices and recap need processing.", confirm: e.estimate_usd ?? 0 });
    else if (e?.status === "refused" || e?.status === "error") setNote(b.id, { id: b.id, text: e.message ?? "Character processing did not run." });
    else setNote(b.id, null);
    await load();
  }

  async function move(b: BookSummary) {
    const ctl = new AbortController();
    abortRef.current?.abort();
    abortRef.current = ctl;
    setMoving(true);
    setNote(b.id, null);
    try {
      const st = await changeKind(b.id, moveTarget(b));
      await load();
      setMoving(false);
      if (!ctl.signal.aborted) await follow(b, st, ctl);
    } catch (err) {
      if (!ctl.signal.aborted) setNote(b.id, { id: b.id, text: (err as Error).message });
    } finally {
      setMoving(false);
    }
  }

  async function confirmProcessing(b: BookSummary) {
    const ctl = new AbortController();
    abortRef.current?.abort();
    abortRef.current = ctl;
    setNote(b.id, { id: b.id, text: "Starting", progress: 0 });
    try {
      const st = await startEnrichment(b.id);
      if (!ctl.signal.aborted) await follow(b, st, ctl);
    } catch (err) {
      if (!ctl.signal.aborted) setNote(b.id, { id: b.id, text: (err as Error).message });
    }
  }

  const shelves = books ? groupShelves(books) : null;
  const volume = (b: BookSummary) => (
    <Volume key={b.id} b={b} busy={moving} note={notes[b.id]} onMove={move} onConfirm={confirmProcessing} />
  );

  return (
    <main className="library">
      <header className="masthead">
        <h1>Talkback</h1>
        {accountLabel(me) && (
          <p className="account">{accountLabel(me)} · <a href={SIGN_OUT_URL}>Sign out</a></p>
        )}
        <p>Books read aloud in full cast. Stop anywhere, ask a question out loud, and the story picks up from the same sentence.</p>
      </header>

      {error && (
        <p className="state-line" role="alert">
          The library could not be reached ({error}). Start the backend with <code>uv run uvicorn app.main:app --port 8000</code>.
        </p>
      )}
      {!books && !error && <p className="state-line">Opening the shelves…</p>}

      {shelves && (
        <>
          <section className="shelf-section" aria-labelledby="shelf-novels">
            <h2 id="shelf-novels" className="shelf-heading">Novels</h2>
            {shelves.series.length === 0 && shelves.standalone.length === 0 && (
              <p className="shelf-empty">No novels yet. Upload a PDF or text file.</p>
            )}
            {shelves.series.map((g) => (
              <div key={g.series} className="series-group" role="group" aria-label={seriesTitle(g.series)}>
                <h3 className="series-heading">{seriesTitle(g.series)}</h3>
                <div className="shelf">{g.books.map(volume)}</div>
              </div>
            ))}
            {shelves.standalone.length > 0 && <div className="shelf">{shelves.standalone.map(volume)}</div>}
          </section>

          <section className="shelf-section" aria-labelledby="shelf-textbooks">
            <h2 id="shelf-textbooks" className="shelf-heading">Textbooks</h2>
            {shelves.textbooks.length === 0
              ? <p className="shelf-empty">No textbooks yet. Upload a PDF or text file.</p>
              : <div className="shelf">{shelves.textbooks.map(volume)}</div>}
          </section>
        </>
      )}

      <AddBook onReady={load} />

      <p className="licence">
        The Harry Potter series is read from your own copy. It is never committed to git; an online copy of this app needs an access code to open the books.
      </p>
      {hasCode && (
        <p className="forget-code">
          <button className="link-btn" onClick={() => { clearAccessToken(); setHasCode(false); }}>Forget access code on this device</button>
        </p>
      )}
    </main>
  );
}
