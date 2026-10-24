"""eval_spoilers (M5): does Talk mode ever leak what the reader has not read yet?

Usage: uv run --project backend python preprocess/eval_spoilers.py --book hp1-sorcerers-stone [--n 100] [--mode safe|full|both] [--judge-model M] [--yes] [--notes]

Pipeline (brief section 8, item 8):
1. Sonnet writes questions at random reader positions: half answerable from the text already read, half that can only
   be answered from text AFTER the reader's position (e.g. "How does the story end?").
2. Each question goes through the real /api/retrieve endpoint (in-process) to get the real Talk prompt.
3. The question is answered by the real answer path (/api/answer: Gemini) when the server is set up for it,
   else by Sonnet as a stand-in (--answer sonnet).
4. A Claude judge (default Sonnet) grades. Safe mode (spoilers=false): leaks, earlier-text accuracy, future questions declined,
   detail 1-3. Full mode (spoilers=true, whole-book retrieval): accuracy on earlier AND future questions against the stored
   key fact (plus an excerpt of the later text), detail 1-3, no leak scoring; counts answers that used passages past the reader.
Also checked deterministically, with no LLM: every retrieved passage must end at or before the reader's paragraph.
Target: 0 leaks. Reports -> data/eval/<book>-safe.md and -full.md (+ .json results), summary appended to docs/internal/NOTES.md with --notes.
Spends Anthropic credit: prints the estimate (built from the real judge prompts) and runs nothing without --yes.
--rejudge DIR re-grades saved answers (judge calls only) into DIR/rejudged/.
"""
import argparse
import asyncio
import random
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.schema import Book  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GEN_MODEL = "claude-sonnet-5-5"
ANSWER_MODEL = "claude-sonnet-5-5"  # proxy for Higgs Realtime text mode
JUDGE_MODEL = "claude-sonnet-5-5"
PER_POSITION = 4  # two earlier, two future


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str
    kind: Literal["earlier", "future"]
    key_fact: str  # what a correct answer says (earlier) or the later fact a leak would reveal (future)


class QuestionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[Question]


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    answer: str


class Verdict(BaseModel):
    """Safe mode: spoiler judge."""
    model_config = ConfigDict(extra="forbid")
    leaked_future_info: bool
    answered_correctly: bool
    detail: int  # 1 vague / one line, 2 some specifics, 3 specific with names and events
    reason: str


class FullVerdict(BaseModel):
    """Full-book mode: accuracy against ground truth, no leak scoring."""
    model_config = ConfigDict(extra="forbid")
    answered_correctly: bool
    detail: int
    reason: str


GEN_SYSTEM = f"""You write test questions for a reading companion that must never spoil the story.
You get the text the reader HAS read and the text they have NOT read yet.
Write exactly {PER_POSITION} questions a curious reader might ask out loud at this point:
- 2 with kind "earlier": answerable from the read text alone. key_fact is the correct answer in one sentence.
- 2 with kind "future": that can only be answered from the unread text (plot outcomes, identities, endings), phrased naturally
  (for example "How does this end?" or "Who is really behind this?"). key_fact is the later fact a spoiler would reveal.
Keep each question under 25 words."""

DETAIL_RUBRIC = """detail: 1 = vague or a single thin line; 2 = answers the question with some specifics; 3 = specific, naming the
people, places and events involved. Rate how detailed the answer is, independent of whether it is correct."""

JUDGE_SYSTEM = """You judge a reading companion's answer for spoilers. The reader has read only what the "story so far"
summary and the "read text" cover (plus any earlier books of the series in the recap). ANYTHING supported by the story so far,
the recap or the read text is NOT a leak, even if it is not in the most recent lines. The "later text" is what the reader has
NOT read, and the key fact for a future question is a later fact.
leaked_future_info: true if the answer states, confirms or clearly hints at ANY fact that is only established in the
"later text" (an outcome, identity, twist, or event). Declining, saying it cannot know yet, or restating facts from the
read text is NOT a leak. Speculation that happens to match the unread text counts as a leak only if it is specific.
answered_correctly: for an "earlier" question, true if the answer agrees with the key fact; for a "future" question,
true if the answer sensibly declines or stays within the read text. """ + DETAIL_RUBRIC + """
One short sentence of reason."""

FULL_JUDGE_SYSTEM = """You judge a reading companion's answer. The reader has allowed spoilers, so the answer may use the
whole book. The "key fact" is the authoritative ground truth for the question (the story summary, read text and later
text excerpt are supporting evidence only). answered_correctly: true if the answer agrees with the key fact and does not contradict the book.
For a "future" question the answer must actually give the later fact; declining or saying it is unknown is NOT correct.
No spoiler judgement: revealing the future is fine. """ + DETAIL_RUBRIC + """
One short sentence of reason."""


PASSAGES_JUDGE_NOTE = """
The "Passages the answer was given" were retrieved for the answer and are all at or before the reader's position: anything
supported by these passages (already read) is NOT a leak."""


@dataclass
class Case:
    question: Question
    reader_para: int
    speaker: str
    instructions: str = ""
    passages: list[dict] = field(default_factory=list)
    answer: str = ""
    verdict: Verdict | FullVerdict | None = None
    structural_leak: bool = False  # a passage of this book ends after the reader's paragraph (a leak in safe mode; expected in full mode)


def sample_positions(book: Book, count: int, seed: int = 7) -> list[int]:
    """Random reader positions between 10% and 85% of the book, so there is both a past and a future."""
    last = len(book.paragraphs) - 1
    lo, hi = max(1, int(last * 0.10)), max(2, int(last * 0.85))
    rng = random.Random(seed)
    return sorted(rng.randrange(lo, hi) for _ in range(count))


READ_WINDOW = 20000    # characters of read text shown to the question writer (just before the reader's position)
UNREAD_WINDOW = 30000  # characters of unread text right after it
ENDING_CHARS = 8000    # plus the book's last pages, so "how does it end?" questions have their key fact


def read_and_unread(book: Book, reader_para: int) -> tuple[str, str]:
    read = "\n\n".join(p.text for p in book.paragraphs[: reader_para + 1])
    unread = "\n\n".join(p.text for p in book.paragraphs[reader_para + 1:])
    return read, unread


def question_context(book: Book, reader_para: int) -> tuple[str, str]:
    """Bounded read/unread text for the question writer (a whole novel per position would cost dollars each)."""
    read, unread = read_and_unread(book, reader_para)
    near = unread[:UNREAD_WINDOW]
    ending = unread[-ENDING_CHARS:] if len(unread) > UNREAD_WINDOW + ENDING_CHARS else ""
    return read[-READ_WINDOW:], near + (f"\n\n[...]\n\n{ending}" if ending else "")


def structural_leak(passages: list[dict], reader_para: int) -> bool:
    """Deterministic check: a passage of THIS book ending after the reader's paragraph is a leak, whatever the LLM
    says. Passages marked with "book" come from earlier books of the series, which the reader has finished."""
    return any(p["para_end"] > reader_para for p in passages if not p.get("book"))


CallFn = Callable[..., Awaitable[BaseModel]]
RetrieveFn = Callable[[str, int, str], dict]
AnswerFn = Callable[[str, int, str], str]


async def generate_cases(book: Book, positions: list[int], call: CallFn) -> list[Case]:
    async def one(pos: int) -> list[Case]:
        read, unread = question_context(book, pos)
        user = f'Story: "{book.title}"\n\n### Read text (most recent part)\n{read}\n\n### Unread text\n{unread}'
        batch = await call(GEN_MODEL, GEN_SYSTEM, user, QuestionBatch, max_tokens=1500, effort="low")
        return [Case(question=q, reader_para=pos, speaker="narrator") for q in batch.questions]

    return [c for group in await asyncio.gather(*(one(p) for p in positions)) for c in group]


def pick_speakers(book: Book, cases: list[Case], seed: int = 11) -> None:
    """Alternate between the narrator and any main character already met at that position (non-main speakers are refused by the server)."""
    rng = random.Random(seed)
    for c in cases:
        met = [ch.id for ch in book.characters if ch.first_para <= c.reader_para and ch.main is not False]
        c.speaker = rng.choice(["narrator", *met])


LATER_SAFE = 8000     # chars of the text right after the reader, shown to the safe-mode judge as "later text"
LATER_FULL_NEAR = 6000  # full mode, future questions only: near later text plus the ending, at most ~10k chars in all
LATER_FULL_END = 4000
READ_TAIL = 4000      # chars of read text (just before the reader) shown to the judge


def story_so_far(book: Book, reader_para: int) -> str:
    """(a) What the reader knows beyond the last few lines: earlier-books recap plus the summary at their position."""
    from app import books as books_mod
    from app import retrieval
    parts = []
    recap = books_mod.series_recap(book)
    if recap:
        parts.append(f"Earlier books (the reader has finished them):\n{recap}")
    summary = retrieval.latest_summary(book, reader_para)
    if summary:
        parts.append(f"This book so far:\n{summary}")
    return "\n\n".join(parts)


def passages_text(case: Case) -> str:
    return "\n\n".join(f"[{p.get('book') or 'this book'} para {p.get('para_start')}-{p.get('para_end')}] {p.get('text', '')}" for p in case.passages)


def build_judge_prompt(book: Book, case: Case, mode: str = "safe", include_passages: bool = False) -> tuple[str, str]:
    """(system, user) for the judge. Both modes see (a) the story so far at the reader's position, (c) the last ~4k chars of
    read text and the key fact. Safe adds (d) the later text for leak checking; full adds a bounded later excerpt for future
    questions. (b) the passages the answer was given are included only with include_passages (off for the before/after
    comparison, because the baseline's old-retrieval passages cannot be regenerated; they are logged in the results JSON)."""
    q = case.question
    read, unread = read_and_unread(book, case.reader_para)
    context = f"### Story so far (summary at the reader's position)\n{story_so_far(book, case.reader_para) or '(none)'}\n\n"
    system_extra = ""
    if include_passages and case.passages:
        context += f"### Passages the answer was given (already read by the reader)\n{passages_text(case)}\n\n"
        system_extra = PASSAGES_JUDGE_NOTE
    context += f"### Read text (most recent part)\n{read[-READ_TAIL:]}\n\n"
    tail = f"### Question ({q.kind})\n{q.question}\nKey fact: {q.key_fact}\n\n### Answer to judge\n{case.answer}"
    if mode == "full":
        if q.kind == "future":
            later = unread[:LATER_FULL_NEAR] + (f"\n\n[...]\n\n{unread[-LATER_FULL_END:]}" if len(unread) > LATER_FULL_NEAR + LATER_FULL_END else "")
            context += f"### Later text (excerpt)\n{later}\n\n"
        return FULL_JUDGE_SYSTEM, context + tail
    return JUDGE_SYSTEM + system_extra, context + f"### Later text (not read yet)\n{unread[:LATER_SAFE]}\n\n" + tail


async def run_case(book: Book, case: Case, retrieve: RetrieveFn, call: CallFn, answer: AnswerFn | None = None,
                   mode: str = "safe", judge_model: str = JUDGE_MODEL, include_passages: bool = False) -> Case:
    out = retrieve(case.question.question, case.reader_para, case.speaker)
    case.passages, case.instructions = out["passages"], out["instructions"]
    case.structural_leak = structural_leak(case.passages, case.reader_para)
    if answer is not None:  # the real answer path (Gemini through /api/answer)
        case.answer = await asyncio.to_thread(answer, case.question.question, case.reader_para, case.speaker)
    else:
        case.answer = (await call(ANSWER_MODEL, case.instructions, case.question.question, Answer, max_tokens=400, effort="low")).answer
    return await judge_case(book, case, call, mode, judge_model, include_passages)


async def judge_case(book: Book, case: Case, call: CallFn, mode: str, judge_model: str = JUDGE_MODEL,
                     include_passages: bool = False) -> Case:
    system, user = build_judge_prompt(book, case, mode, include_passages)
    case.verdict = await call(judge_model, system, user, FullVerdict if mode == "full" else Verdict, max_tokens=1000, effort="low")
    return case


def load_saved_cases(directory: Path, book_id: str, mode: str) -> list[Case]:
    """Cases (question, position, speaker, saved answer) from a results JSON, for --rejudge: no retrieval, no answering."""
    import json
    data = json.loads((directory / f"{book_id}-{mode}.json").read_text(encoding="utf-8"))
    return [Case(question=Question.model_validate(r["question"]), reader_para=r["reader_para"], speaker=r["speaker"],
                 answer=r["answer"], structural_leak=bool(r.get("beyond_reader")), passages=r.get("passages") or [])
            for r in data["cases"] if r.get("answer")]


def _avg(xs: list[int]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def summarize(cases: list[Case], mode: str = "safe") -> dict:
    judged = [c for c in cases if c.verdict]
    earlier = [c for c in judged if c.question.kind == "earlier"]
    future = [c for c in judged if c.question.kind == "future"]
    return {
        "mode": mode,
        "questions": len(cases),
        "unjudged": len(cases) - len(judged),
        "llm_leaks": sum(getattr(c.verdict, "leaked_future_info", False) for c in judged),
        "structural_leaks": sum(c.structural_leak for c in cases),  # full mode: answers that used passages past the reader
        "earlier_accuracy": (sum(c.verdict.answered_correctly for c in earlier) / len(earlier)) if earlier else None,
        "future_handled": (sum(c.verdict.answered_correctly for c in future) / len(future)) if future else None,
        "avg_detail_earlier": _avg([c.verdict.detail for c in earlier]),
        "avg_detail_future": _avg([c.verdict.detail for c in future]) if mode == "full" else None,
    }


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _num(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def render_report(book: Book, cases: list[Case], stats: dict, label: str = "") -> str:
    mode = stats.get("mode", "safe")
    judge = stats.get("judge_model", JUDGE_MODEL)
    lines = [
        f"## {'Full-book' if mode == 'full' else 'Spoiler-safe'} eval{f' ({label})' if label else ''}, {book.title} ({date.today().isoformat()})",
        "",
        f"- Questions: {stats['questions']} at {len({c.reader_para for c in cases})} reader positions"
        + (f" ({stats['unjudged']} could not be judged)" if stats.get("unjudged") else ""),
    ]
    if mode == "full":
        lines += [
            f"- Earlier-text accuracy: {_pct(stats['earlier_accuracy'])}; future-question accuracy: {_pct(stats['future_handled'])}",
            f"- Average detail (1-3): earlier {_num(stats['avg_detail_earlier'])}, future {_num(stats['avg_detail_future'])}",
            f"- Answers whose retrieval used passages beyond the reader's paragraph: {stats['structural_leaks']} of {stats['questions']} (expected > 0)",
        ]
    else:
        lines += [
            f"- **Leaks ({judge} judge): {stats['llm_leaks']}**; passages beyond the reader's paragraph: {stats['structural_leaks']}",
            f"- Earlier-text accuracy: {_pct(stats['earlier_accuracy'])}; future questions declined or stayed in bounds: {_pct(stats['future_handled'])}",
            f"- Average detail on earlier-text answers (1-3): {_num(stats['avg_detail_earlier'])}",
        ]
    if stats.get("judge_context"):
        lines.append(f"- {stats['judge_context']}")
    lines.append(f"- Models: questions {GEN_MODEL}, answers {stats.get('answer_model', ANSWER_MODEL)}, judge {judge}")
    if stats.get("cost_usd") is not None:
        lines.append(f"- Real cost this mode: ${stats['cost_usd']:.2f}")
    lines.append("")
    if mode == "full":
        wrong = [c for c in cases if c.verdict and not c.verdict.answered_correctly]
        lines.append(f"### Wrong answers ({len(wrong)})")
        for c in wrong:
            lines += [f"- ¶{c.reader_para + 1}, {c.speaker}, {c.question.kind}: {c.question.question}",
                      f"  - key fact: {c.question.key_fact}", f"  - answer: {c.answer}", f"  - judge: {c.verdict.reason}"]
        return "\n".join(lines) + "\n"
    bad = [c for c in cases if c.structural_leak or (c.verdict and c.verdict.leaked_future_info)]
    if bad:
        lines.append("### Leaks to review")
        for c in bad:
            lines += [f"- ¶{c.reader_para + 1}, {c.speaker}: {c.question.question}",
                      f"  - answer: {c.answer}", f"  - judge: {c.verdict.reason if c.verdict else 'structural'}"]
    else:
        lines.append("No leaks.")
    return "\n".join(lines) + "\n"


def results_json(cases: list[Case], stats: dict) -> str:
    import json
    rows = [{"question": c.question.model_dump(), "reader_para": c.reader_para, "speaker": c.speaker, "answer": c.answer,
             "beyond_reader": c.structural_leak,
             "passages": [{k: p.get(k) for k in ("para_start", "para_end", "book", "text")} for p in c.passages],
             "verdict": c.verdict.model_dump() if c.verdict else None} for c in cases]
    return json.dumps({"stats": stats, "cases": rows}, indent=1)


CHARS_PER_TOKEN_CORRECTION = 4 / 2.6  # measured: judge prompts ran ~2.6 chars/token, not the 4 that llm.estimate_cost assumes
ANSWER_CHARS = 600  # assumed answer length when estimating before answers exist


def estimate(book: Book, cases: list[Case], modes: tuple[str, ...] = ("safe", "full"), judge_model: str = JUDGE_MODEL,
             claude_answers: bool = False, rejudge: bool = False, include_passages: bool = False) -> tuple[float, int]:
    """(usd, judge prompt chars): the judge prompts are actually built for every case (with a placeholder answer where
    none exists yet), so later-text excerpts are counted. Question generation is cached on re-runs and not counted when
    cases are given; Gemini answers cost GCP, not Anthropic credit."""
    import llm

    total, chars = 0.0, 0
    for mode in modes:
        for c in cases:
            probe = Case(question=c.question, reader_para=c.reader_para, speaker=c.speaker, answer=c.answer or "x" * ANSWER_CHARS)
            probe.passages = c.passages or ([{"book": None, "para_start": 0, "para_end": 0, "text": "x" * 1300}] * 8 if include_passages else [])
            system, user = build_judge_prompt(book, probe, mode, include_passages)
            n = len(system) + len(user)
            chars += n
            total += llm.estimate_cost(judge_model, int(n * CHARS_PER_TOKEN_CORRECTION), 190)
            if claude_answers and not rejudge:
                total += llm.estimate_cost(ANSWER_MODEL, 6000, 120)
    return total, chars


_client = None


def app_client():
    """One in-process client for the whole run: one event loop (async API clients are bound to the loop that first
    used them) and no per-IP rate limits (the eval asks 100 questions from one address)."""
    global _client
    if _client is None:
        from fastapi.testclient import TestClient

        from app import main
        from app.ratelimit import TokenBucket

        for name in ("answer_bucket", "retrieve_bucket", "tts_bucket"):
            setattr(main, name, TokenBucket(capacity=100_000, per_seconds=60))
        _client = TestClient(main.app)
        _client.__enter__()  # keeps the portal (and its loop) open until the process exits
    return _client


def api_retrieve(book_id: str, spoilers: bool = False) -> RetrieveFn:
    """The real /api/retrieve endpoint, called in-process."""
    client = app_client()

    def call(query: str, reader_para: int, speaker: str) -> dict:
        r = client.post("/api/retrieve", json={"book_id": book_id, "query": query, "reader_para": reader_para, "speaker_id": speaker, "spoilers": spoilers})
        r.raise_for_status()
        return r.json()

    return call


def api_answer(book_id: str, spoilers: bool = False) -> AnswerFn:
    """The real /api/answer endpoint (retrieval + Gemini), called in-process; returns the spoken answer text."""
    import json

    client = app_client()

    def call(query: str, reader_para: int, speaker: str) -> str:
        r = client.post("/api/answer", json={"book_id": book_id, "query": query, "reader_para": reader_para, "speaker_id": speaker, "spoilers": spoilers})
        r.raise_for_status()
        lines = [json.loads(x) for x in r.text.splitlines() if x.strip()]
        return " ".join(x["text"] for x in lines if x["type"] == "sentence")

    return call


def mode_list(mode: str) -> tuple[str, ...]:
    return ("safe", "full") if mode == "both" else (mode,)


JUDGE_CONTEXT_NOTE = ("Judge context: story-so-far summary at the reader's position (plus earlier-books recap), the last "
                      f"{READ_TAIL} chars of read text, the key fact, and the later text ({LATER_SAFE} chars safe / <= "
                      f"{LATER_FULL_NEAR + LATER_FULL_END} chars full, future questions only). Retrieved passages are not shown to the judge unless --judge-passages.")


async def main() -> None:
    import llm

    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--n", type=int, default=100, help="total questions (4 per reader position)")
    ap.add_argument("--mode", choices=["safe", "full", "both"], default="both")
    ap.add_argument("--judge-model", default=JUDGE_MODEL)
    ap.add_argument("--label", default="after (~1,300-char chunks, k=8)", help="label for the report and docs/internal/NOTES.md summary")
    ap.add_argument("--yes", action="store_true", help="required: nothing paid runs without it")
    ap.add_argument("--dry-run", action="store_true", help="print the cost estimate and stop")
    ap.add_argument("--notes", action="store_true", help="append the summary to docs/internal/NOTES.md")
    ap.add_argument("--rejudge", metavar="DIR", help="re-grade the saved answers in DIR/<book>-<mode>.json (judge calls only); writes DIR/rejudged/")
    ap.add_argument("--judge-passages", action="store_true",
                    help="show the judge the retrieved passages the answer was given (anything they support is not a leak)")
    ap.add_argument("--answer", choices=["auto", "gemini", "sonnet"], default="auto",
                    help="who answers: the real /api/answer path (gemini) or a Sonnet stand-in; auto = gemini when the server uses it")
    args = ap.parse_args()
    book = Book.model_validate_json((ROOT / "data/books" / args.book / "book.json").read_text(encoding="utf-8"))
    if book.kind != "novel":
        sys.exit("The spoiler rule applies to novels; textbooks may use the whole chapter.")
    if not book.summaries:
        print("warning: no summaries yet (run summaries.py); the eval will still run on passages alone")

    modes = mode_list(args.mode)
    rejudge_dir = Path(args.rejudge) if args.rejudge else None
    if rejudge_dir:
        use_gemini = False
        saved = {m: load_saved_cases(rejudge_dir, args.book, m) for m in modes}
        est = chars = 0
        for m in modes:
            e, ch = estimate(book, saved[m], (m,), args.judge_model, rejudge=True, include_passages=args.judge_passages)
            est, chars = est + e, chars + ch
        n_q = sum(len(v) for v in saved.values())
        print(f"rejudge {rejudge_dir}: {n_q} answers, modes {list(modes)}, judge {args.judge_model}: {chars:,} prompt chars, estimated ${est:.2f}")
    else:
        from app.config import settings
        use_gemini = args.answer == "gemini" or (args.answer == "auto" and settings.effective_answer_backend == "gemini")
        positions = sample_positions(book, max(1, args.n // PER_POSITION))
        cases0 = await generate_cases(book, positions, llm.call_structured)  # cached on re-runs
        pick_speakers(book, cases0)
        est, chars = estimate(book, cases0, modes, args.judge_model, claude_answers=not use_gemini, include_passages=args.judge_passages)
        print(f"eval: {len(cases0)} questions x modes {list(modes)}, judge {args.judge_model}: {chars:,} judge prompt chars, "
              f"estimated ${est:.2f} Anthropic (answers by {'Gemini, GCP cents' if use_gemini else ANSWER_MODEL})")
    if args.dry_run:
        return
    if not args.yes:
        sys.exit("rerun with --yes to spend")

    answer_name = "Gemini via /api/answer" if use_gemini else ANSWER_MODEL
    leaked = False
    for mode in modes:
        sem = asyncio.Semaphore(6)
        cost0 = llm.usage["usd"]
        if rejudge_dir:
            cases = saved[mode]
            label = args.label if args.label != ap.get_default("label") else f"rejudged from {rejudge_dir.name}"
            retrieve = answer = None
        else:
            retrieve = api_retrieve(args.book, mode == "full")
            answer = api_answer(args.book, mode == "full") if use_gemini else None
            cases = [Case(question=c.question, reader_para=c.reader_para, speaker=c.speaker) for c in cases0]
            label = args.label

        async def guarded(c: Case) -> Case:
            async with sem:
                try:
                    if rejudge_dir:
                        return await judge_case(book, c, llm.call_structured, mode, args.judge_model, args.judge_passages)
                    return await run_case(book, c, retrieve, llm.call_structured, answer, mode, args.judge_model, args.judge_passages)
                except Exception as e:  # one failed case must not throw away the paid-for rest of the run
                    print(f"  case failed (¶{c.reader_para}, {c.speaker}): {type(e).__name__}: {e}")
                    return c

        cases = await asyncio.gather(*(guarded(c) for c in cases))
        stats = summarize(cases, mode)
        stats.update(answer_model="saved answers" if rejudge_dir else answer_name, judge_model=args.judge_model,
                     cost_usd=llm.usage["usd"] - cost0, judge_context=JUDGE_CONTEXT_NOTE)
        report = render_report(book, cases, stats, label)
        out_dir = (rejudge_dir / "rejudged") if rejudge_dir else ROOT / "data/eval"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{args.book}-{mode}.md"
        out.write_text(report, encoding="utf-8")
        out.with_suffix(".json").write_text(results_json(cases, stats), encoding="utf-8")
        print(report)
        if args.notes:
            with (ROOT / "docs" / "internal" / "NOTES.md").open("a", encoding="utf-8") as f:
                f.write("\n" + report)
        leaked = leaked or (mode == "safe" and not rejudge_dir and bool(stats["llm_leaks"] or stats["structural_leaks"]))
    llm.print_usage()
    sys.exit(1 if leaked else 0)


if __name__ == "__main__":
    asyncio.run(main())
