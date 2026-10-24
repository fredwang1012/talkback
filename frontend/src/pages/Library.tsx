import { useEffect, useRef, useState, type DragEvent, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import {
  accountLabel, changeKind, deleteBook, fetchBooks, fetchHiddenBooks, hideBook, restoreBook, waitForDeletion, type HiddenBook, fetchMe, SIGN_OUT_URL, type Me, pollEnrichment, pollUpload, startEnrichment, uploadBook,
  type BookStatus, type BookSummary, type UploadKind,
  enrichDoneNote,
} from "../api";
import { clearAccessToken, getAccessToken } from "../accessToken";
import { canDelete, cardActions, groupShelves, hiddenLabel, kindLabel, moveTarget, seriesBlurb, seriesTitle, wrapIndex, type CardAction, type SeriesGroup } from "../shelves";
import { fileProblem, isBusyError, jobLine, jobProgress, titleFromFile, uploadSettled, type UploadJob } from "../uploads";

const pct = (p: number | undefined) => Math.round(Math.max(0, Math.min(1, p ?? 0)) * 100);

/** What the form hands over: one entry per file (a single file keeps the title typed in the form). */
export type UploadRequest = { files: { file: File; title: string }[]; author: string; kind: UploadKind };

function AddBook({ onStart, onCancel }: { onStart: (req: UploadRequest) => void; onCancel: () => void }) {
  const [files, setFiles] = useState<File[]>([]);
  const [title, setTitle] = useState("");
  const [author, setAuthor] = useState("");
  const [kind, setKind] = useState<UploadKind>("auto");
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  function pick(list: FileList | File[] | null) {
    const picked = list ? [...list] : [];
    setFiles(picked);
    setTitle(picked.length === 1 ? titleFromFile(picked[0].name) : "");
    setError(null);
  }

  function drop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    pick(e.dataTransfer.files);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    if (files.length === 0) return setError("Choose a PDF or .txt file first.");
    const bad = files.map((f) => [f, fileProblem(f)] as const).find(([, p]) => p);
    if (bad) return setError(files.length > 1 ? `${bad[0].name}: ${bad[1]}` : bad[1]);
    if (files.length === 1 && !title.trim()) return setError("Give the book a title.");
    onStart({
      files: files.map((f) => ({ file: f, title: files.length === 1 ? title.trim() : titleFromFile(f.name) || f.name })),
      author: author.trim(), kind,
    });
    setFiles([]); setTitle(""); setAuthor(""); setError(null);
    if (fileRef.current) fileRef.current.value = "";
  }

  return (
    <section className="add-book" aria-labelledby="add-book-h">
      <div className="add-book-head">
        <h2 id="add-book-h">Add a book</h2>
        <button type="button" className="job-dismiss" onClick={onCancel} aria-label="Close">×</button>
      </div>
      <form onSubmit={submit}>
        <div className={`dropzone${dragging ? " dropzone-over" : ""}`}
          onDragOver={(e) => { e.preventDefault(); setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={drop}>
          <label htmlFor="ab-file">PDF or text files (up to 20 MB each). Choose several, or drop them here.</label>
          <input id="ab-file" ref={fileRef} type="file" accept=".pdf,.txt" multiple onChange={(e) => pick(e.target.files)} />
          {files.length > 1 && (
            <ul className="dropzone-files">{files.map((f) => <li key={f.name + f.size}>{f.name}</li>)}</ul>
          )}
        </div>
        {files.length <= 1 && (
          <div className="field">
            <label htmlFor="ab-title">Title</label>
            <input id="ab-title" type="text" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} />
          </div>
        )}
        <div className="field">
          <label htmlFor="ab-author">Author (optional{files.length > 1 ? ", used for every file" : ""})</label>
          <input id="ab-author" type="text" value={author} maxLength={120} onChange={(e) => setAuthor(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="ab-kind">Kind</label>
          <select id="ab-kind" value={kind} onChange={(e) => setKind(e.target.value as UploadKind)}>
            <option value="auto">Auto-detect</option>
            <option value="novel">Novel</option>
            <option value="textbook">Textbook</option>
          </select>
        </div>
        <div className="add-book-actions">
          <button type="submit" className="add-book-go">{files.length > 1 ? `Add ${files.length} books` : "Add the book"}</button>
          <button type="button" className="volume-move" onClick={onCancel}>Cancel</button>
        </div>
      </form>
      {error && <p className="add-book-error" role="alert">{error}</p>}
      <p className="add-book-note">The file stays on this computer. With cloud search on, the book's text is sent to Google Cloud to make it searchable.</p>
    </section>
  );
}

/** One card per upload: its own stage, indexing count or queue place, and the confirm button in testing mode. */
function JobCard({ j, onConfirm, onDismiss }: { j: UploadJob; onConfirm: (j: UploadJob) => void; onDismiss: (j: UploadJob) => void }) {
  const progress = jobProgress(j);
  const finished = j.phase === "ready" || j.phase === "error";
  return (
    <li className={`job job-${j.phase}`}>
      <div className="job-head">
        <span className="job-title">{j.title}</span>
        {finished && (
          <button type="button" className="job-dismiss" onClick={() => onDismiss(j)} aria-label={`Dismiss ${j.title}`}>×</button>
        )}
      </div>
      <p className="job-line">{jobLine(j)}</p>
      {progress !== undefined && <progress max={100} value={pct(progress)} aria-label={`${j.title}: ${jobLine(j)}`} />}
      {j.phase === "confirm" && (
        <button type="button" className="volume-move" onClick={() => onConfirm(j)}>
          Process with characters (~${(j.estimate ?? 0).toFixed(2)})
        </button>
      )}
      {j.bookId && j.phase !== "error" && j.status?.status === "ready" && <a className="job-open" href={`#/read/${j.bookId}`}>Open</a>}
    </li>
  );
}

const sleep = (ms: number, signal: AbortSignal) => new Promise<void>((resolve) => {
  const t = setTimeout(done, ms);
  function done() { clearTimeout(t); signal.removeEventListener("abort", done); resolve(); }
  signal.addEventListener("abort", done);
});

/** An uploaded book whose card is already gone while the server finishes removing it (search entries, files). */
type Removal = { book: BookSummary; state: "running" | "error"; message?: string };

type MoveNote = { id: string; text: string; progress?: number; confirm?: number };

/** A card's "…" menu (menu button pattern): arrow keys move, Esc closes and returns focus, a click outside closes. */
function CardMenu({ b, busy, onPick }: { b: BookSummary; busy: boolean; onPick: (a: CardAction) => void }) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const items = useRef<(HTMLButtonElement | null)[]>([]);
  const actions = cardActions(b);
  const menuId = `menu-${b.id}`;

  useEffect(() => {
    if (!open) return;
    items.current[0]?.focus();
    const outside = (e: MouseEvent) => { if (!wrap.current?.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", outside);
    return () => document.removeEventListener("mousedown", outside);
  }, [open]);

  function close() { setOpen(false); button.current?.focus(); }

  function onKey(e: KeyboardEvent<HTMLDivElement>) {
    const i = items.current.findIndex((el) => el === document.activeElement);
    if (e.key === "Escape") { e.preventDefault(); close(); }
    else if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      items.current[wrapIndex(i, e.key === "ArrowDown" ? 1 : -1, actions.length)]?.focus();
    } else if (e.key === "Home") { e.preventDefault(); items.current[0]?.focus(); }
    else if (e.key === "End") { e.preventDefault(); items.current[actions.length - 1]?.focus(); }
    else if (e.key === "Tab") setOpen(false);
  }

  return (
    <div className="card-menu" ref={wrap} onKeyDown={onKey}>
      <button type="button" ref={button} className="card-menu-btn" disabled={busy} aria-haspopup="menu" aria-expanded={open}
        aria-controls={open ? menuId : undefined} aria-label={`Actions for ${b.title}`} onClick={() => setOpen((v) => !v)}>
        <span aria-hidden="true">…</span>
      </button>
      {open && (
        <div className="card-menu-list" role="menu" id={menuId} aria-label={`Actions for ${b.title}`}>
          {actions.map((a, i) => (
            <button key={a.id} type="button" role="menuitem" tabIndex={-1} ref={(el) => { items.current[i] = el; }}
              className={a.id === "remove" ? "card-menu-danger" : undefined}
              onClick={() => { setOpen(false); onPick(a); }}>
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function Volume({ b, busy, note, onMove, onConfirm, onDelete, onHide }: {
  b: BookSummary; busy: boolean; note?: MoveNote; onMove: (b: BookSummary) => void; onConfirm: (b: BookSummary) => void;
  onDelete: (b: BookSummary) => Promise<void>; onHide: (b: BookSummary) => void;
}) {
  const [asking, setAsking] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const pick = (a: CardAction) => {
    if (a.id === "move") onMove(b);
    else if (canDelete(b)) setAsking(true);
    else onHide(b);
  };
  return (
    <div className="volume-wrap">
      <div className="volume-card">
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
        <CardMenu b={b} busy={busy || deleting} onPick={pick} />
      </div>
      {asking && (
        <div className="volume-confirm" role="alertdialog" aria-label={`Delete ${b.title}?`}>
          <span>Delete “{b.title}” for good? The book, its cast settings and its search data are removed. This cannot be undone.</span>
          <div className="volume-confirm-actions">
            <button type="button" className="volume-move volume-delete" disabled={deleting}
              onClick={async () => { setDeleting(true); try { await onDelete(b); } finally { setDeleting(false); setAsking(false); } }}>
              {deleting ? "Deleting…" : "Delete forever"}
            </button>
            <button type="button" className="volume-move" disabled={deleting} autoFocus onClick={() => setAsking(false)}>Cancel</button>
          </div>
        </div>
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

/** Books of one series stacked into a single card; it opens in place to show every volume in series order. */
function SeriesStack({ g, children }: { g: SeriesGroup; children: (b: BookSummary) => ReactNode }) {
  const [open, setOpen] = useState(false);
  const cover = g.books.find((b) => b.cover)?.cover;
  const bodyId = `series-${g.series}`;
  return (
    <div className={`series-stack${open ? " series-open" : ""}`} role="group" aria-label={seriesTitle(g.series)}>
      <button type="button" className="series-card" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen((v) => !v)}>
        {cover && <img className="cover" src={cover} alt="" />}
        <span className="series-card-text">
          <span className="series-card-title">{seriesTitle(g.series)}</span>
          <span className="series-card-blurb">{seriesBlurb(g)}</span>
          <span className="open">{open ? "Close the series" : `Show all ${g.books.length} books`}</span>
        </span>
      </button>
      {open && <div className="shelf series-books" id={bodyId}>{g.books.map(children)}</div>}
    </div>
  );
}

/** The upload form in a modal dialog: native <dialog> gives the inert background, focus containment and Esc; focus
 * goes back to the "Add book" button on close. */
function AddBookDialog({ open, onClose, onStart }: { open: boolean; onClose: () => void; onStart: (req: UploadRequest) => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  return (
    <dialog ref={ref} className="add-dialog" aria-labelledby="add-book-h" onClose={onClose}
      onCancel={(e) => { e.preventDefault(); onClose(); }}
      onMouseDown={(e) => { if (e.target === ref.current) onClose(); }}>
      {open && <AddBook onStart={(req) => { onStart(req); onClose(); }} onCancel={onClose} />}
    </dialog>
  );
}

export default function Library() {
  const [books, setBooks] = useState<BookSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, MoveNote>>({});
  const [moving, setMoving] = useState(false);
  const [hasCode, setHasCode] = useState(() => getAccessToken() !== "");
  const [me, setMe] = useState<Me | null>(null);
  const [hidden, setHidden] = useState<HiddenBook[]>([]);
  const [showHidden, setShowHidden] = useState(false);
  const [jobs, setJobs] = useState<UploadJob[]>([]);
  const [adding, setAdding] = useState(false);
  const addBtn = useRef<HTMLButtonElement>(null);
  const [removals, setRemovals] = useState<Record<string, Removal>>({});
  const alive = useRef<AbortController>(new AbortController());
  const abortRef = useRef<AbortController | null>(null);

  const load = () => fetchBooks().then(setBooks).catch((e: Error) => setError(e.message));
  const loadHidden = () => fetchHiddenBooks().then(setHidden).catch(() => setHidden([]));
  useEffect(() => { load(); loadHidden(); fetchMe().then(setMe); }, []);
  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => { const c = new AbortController(); alive.current = c; return () => c.abort(); }, []);

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

  const setRemoval = (id: string, r: Removal | null) =>
    setRemovals((m) => { const { [id]: _drop, ...rest } = m; return r ? { ...rest, [id]: r } : rest; });

  /** Uploads: the card goes at once; the server removes the search entries and files in the background. */
  async function remove(b: BookSummary) {
    setBooks((list) => (list ? list.filter((x) => x.id !== b.id) : list));
    setNote(b.id, null);
    setRemoval(b.id, { book: b, state: "running" });
    try {
      await deleteBook(b.id);
    } catch (err) {  // refused (still processing, not signed in): nothing was removed, put the card back
      setRemoval(b.id, null);
      await load();
      setNote(b.id, { id: b.id, text: (err as Error).message });
      return;
    }
    const done = await waitForDeletion(b.id, alive.current.signal);
    if (done === null) return;
    if (done.ok) setRemoval(b.id, null);
    else setRemoval(b.id, { book: b, state: "error", message: done.message });
  }

  /** Built-in books: gone from the shelf at once (optimistic), restorable from "Hidden books". */
  async function hide(b: BookSummary) {
    setBooks((list) => (list ? list.filter((x) => x.id !== b.id) : list));
    setHidden((h) => [...h.filter((x) => x.id !== b.id), { id: b.id, title: b.title, author: b.author }]);
    try {
      await hideBook(b.id);
    } catch (err) {
      setHidden((h) => h.filter((x) => x.id !== b.id));
      await load();
      setNote(b.id, { id: b.id, text: (err as Error).message });
    }
  }

  async function restore(h: HiddenBook) {
    setHidden((list) => list.filter((x) => x.id !== h.id));
    try {
      await restoreBook(h.id);
    } catch (err) {
      setHidden((list) => [...list, h]);
      setError((err as Error).message);
    }
    await load();
  }

  // ---- uploads: each file is its own job with its own card ----
  const jobSeq = useRef(0);
  const updJob = (key: string, p: Partial<UploadJob>) => setJobs((js) => js.map((j) => (j.key === key ? { ...j, ...p } : j)));

  /** Poll one uploaded book until it needs nothing more; the shelf refreshes as soon as it is readable. */
  async function followJob(key: string, bookId: string) {
    const signal = alive.current.signal;
    let listed = false;
    const onUpdate = (s: BookStatus) => {
      updJob(key, { phase: "processing", status: s });
      if (s.status === "ready" && !listed) { listed = true; load(); }
    };
    try {
      const st = await pollUpload(bookId, uploadSettled, onUpdate, signal);
      if (!st || signal.aborted) return;
      const e = st.enrich;
      const detected = st.kind_detected ? `Detected: ${kindLabel(st.kind_detected.kind)}` : undefined;
      if (st.status === "error") updJob(key, { phase: "error", status: st, message: st.message || "The book could not be processed." });
      else if (e?.status === "awaiting_confirmation") updJob(key, { phase: "confirm", status: st, estimate: e.estimate_usd ?? 0 });
      else if (e?.status === "done") updJob(key, { phase: "ready", status: st, message: enrichDoneNote(e) });
      else if (e?.status === "refused" || e?.status === "error") updJob(key, { phase: "ready", status: st, message: e.message });
      else updJob(key, { phase: "ready", status: st, message: detected });
      load();
    } catch (err) {
      if (!signal.aborted) updJob(key, { phase: "error", message: (err as Error).message });
    }
  }

  /** Send the files one POST at a time (each is quick); a full server queue or the rate limit means wait and retry.
   * Following runs in parallel: the server indexes a few books at once and queues the rest. */
  async function startUploads(req: UploadRequest) {
    const signal = alive.current.signal;
    const fresh = req.files.map(({ file, title }) => ({ file, title, key: `job-${++jobSeq.current}` }));
    setJobs((js) => [...js, ...fresh.map(({ key, title }): UploadJob => ({ key, title, phase: "waiting", message: "Waiting to upload" }))]);
    for (const { file, title, key } of fresh) {
      for (let attempt = 0; ; attempt++) {
        if (signal.aborted) return;
        updJob(key, { phase: "uploading" });
        try {
          const { book_id } = await uploadBook(file, title, req.author, req.kind);
          updJob(key, { phase: "processing", bookId: book_id, status: { status: "processing", message: "Queued" } });
          void followJob(key, book_id);
          break;
        } catch (err) {
          if (signal.aborted) return;
          if (isBusyError(err) && attempt < 40) {
            updJob(key, { phase: "waiting", message: "Waiting for a free upload slot" });
            await sleep(15000, signal);
            continue;
          }
          updJob(key, { phase: "error", message: (err as Error).message });
          break;
        }
      }
    }
  }

  async function confirmJob(j: UploadJob) {
    if (!j.bookId) return;
    updJob(j.key, { phase: "processing", status: { status: "ready", enrich: { status: "running", message: "Starting", progress: 0 } } });
    try {
      await startEnrichment(j.bookId);
      await followJob(j.key, j.bookId);
    } catch (err) {
      updJob(j.key, { phase: "error", message: (err as Error).message });
    }
  }

  const dismissJob = (j: UploadJob) => setJobs((js) => js.filter((x) => x.key !== j.key));

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
    <Volume key={b.id} b={b} busy={moving} note={notes[b.id]} onMove={move} onConfirm={confirmProcessing} onDelete={remove} onHide={hide} />
  );

  return (
    <main className="library">
      <header className="masthead">
        <h1>Talkback</h1>
        {accountLabel(me) && (
          <p className="account">{accountLabel(me)} · <a href={SIGN_OUT_URL}>Sign out</a></p>
        )}
        <p>Books read aloud in full cast. Stop anywhere, ask a question out loud, and the story picks up from the same sentence.</p>
        <div className="library-actions">
          <button type="button" ref={addBtn} className="add-book-go" aria-haspopup="dialog" onClick={() => setAdding(true)}>Add book</button>
        </div>
      </header>

      {jobs.length > 0 && (
        <section className="jobs" aria-label="Uploads">
          <ul>{jobs.map((j) => <JobCard key={j.key} j={j} onConfirm={confirmJob} onDismiss={dismissJob} />)}</ul>
        </section>
      )}

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
              <p className="shelf-empty">No novels yet. Use Add book to upload a PDF or text file.</p>
            )}
            {(shelves.series.length > 0 || shelves.standalone.length > 0) && (
              <div className="shelf">
                {shelves.series.map((g) => (g.books.length > 1
                  ? <SeriesStack key={g.series} g={g}>{volume}</SeriesStack>
                  : volume(g.books[0])))}
                {shelves.standalone.map(volume)}
              </div>
            )}
          </section>

          <section className="shelf-section" aria-labelledby="shelf-textbooks">
            <h2 id="shelf-textbooks" className="shelf-heading">Textbooks</h2>
            {shelves.textbooks.length === 0
              ? <p className="shelf-empty">No textbooks yet. Use Add book to upload a PDF or text file.</p>
              : <div className="shelf">{shelves.textbooks.map(volume)}</div>}
          </section>
        </>
      )}

      {Object.values(removals).length > 0 && (
        <ul className="removals" aria-label="Books being removed">
          {Object.values(removals).map((r) => (
            <li key={r.book.id} className={r.state === "error" ? "removal-error" : undefined} role="status">
              {r.state === "running"
                ? <span>Removing “{r.book.title}”…</span>
                : <>
                    <span>“{r.book.title}” is off your shelf, but the delete did not finish: {r.message}</span>
                    <button type="button" className="volume-move" onClick={() => remove(r.book)}>Try again</button>
                  </>}
            </li>
          ))}
        </ul>
      )}

      {hidden.length > 0 && (
        <section className="hidden-books" aria-label="Hidden books">
          <button type="button" className="link-btn" aria-expanded={showHidden} onClick={() => setShowHidden((v) => !v)}>
            {hiddenLabel(hidden.length)} · {showHidden ? "Close" : "Restore"}
          </button>
          {showHidden && (
            <ul>
              {hidden.map((h) => (
                <li key={h.id}>
                  <span>{h.title}{h.author && <span className="by"> · {h.author}</span>}</span>
                  <button type="button" className="volume-move" onClick={() => restore(h)} aria-label={`Restore: ${h.title}`}>Restore</button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <AddBookDialog open={adding} onClose={() => { setAdding(false); addBtn.current?.focus(); }} onStart={startUploads} />

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
