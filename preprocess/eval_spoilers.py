"""eval_spoilers (M5): does Talk mode ever leak what the reader has not read yet?

Usage: uv run --project backend python preprocess/eval_spoilers.py --book scandal-in-bohemia [--n 100] [--yes] [--notes]

Pipeline (brief section 8, item 8):
1. Sonnet writes questions at random reader positions: half answerable from the text already read, half that can only
   be answered from text AFTER the reader's position (e.g. "How does the story end?").
2. Each question goes through the real /api/retrieve endpoint (in-process) to get the real Talk prompt.
3. The prompt is answered by Sonnet as a stand-in for Higgs Realtime (the brief allows this proxy).
4. Opus judges: did the answer leak future information? Did it answer earlier questions correctly?
Also checked deterministically, with no LLM: every retrieved passage must end at or before the reader's paragraph.
Target: 0 leaks. Report -> data/eval/<book>-spoilers.md, summary appended to NOTES.md with --notes.
Spends Anthropic credit: nothing runs until you rerun with --yes if the estimate is over $5.
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
JUDGE_MODEL = "claude-opus-5-5"
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
    model_config = ConfigDict(extra="forbid")
    leaked_future_info: bool
    answered_correctly: bool
    reason: str


GEN_SYSTEM = f"""You write test questions for a reading companion that must never spoil the story.
You get the text the reader HAS read and the text they have NOT read yet.
Write exactly {PER_POSITION} questions a curious reader might ask out loud at this point:
- 2 with kind "earlier": answerable from the read text alone. key_fact is the correct answer in one sentence.
- 2 with kind "future": that can only be answered from the unread text (plot outcomes, identities, endings), phrased naturally
  (for example "How does this end?" or "Who is really behind this?"). key_fact is the later fact a spoiler would reveal.
Keep each question under 25 words."""

JUDGE_SYSTEM = """You judge a reading companion's answer for spoilers. The reader has read only the "read text".
leaked_future_info: true if the answer states, confirms or clearly hints at ANY fact that is only established in the
"unread text" (an outcome, identity, twist, or event). Declining, saying it cannot know yet, or restating facts from the
read text is NOT a leak. Speculation that happens to match the unread text counts as a leak only if it is specific.
answered_correctly: for an "earlier" question, true if the answer agrees with the key fact; for a "future" question,
true if the answer sensibly declines or stays within the read text. One short sentence of reason."""


@dataclass
class Case:
    question: Question
    reader_para: int
    speaker: str
    instructions: str = ""
    passages: list[dict] = field(default_factory=list)
    answer: str = ""
    verdict: Verdict | None = None
    structural_leak: bool = False


def sample_positions(book: Book, count: int, seed: int = 7) -> list[int]:
    """Random reader positions between 10% and 85% of the book, so there is both a past and a future."""
    last = len(book.paragraphs) - 1
    lo, hi = max(1, int(last * 0.10)), max(2, int(last * 0.85))
    rng = random.Random(seed)
    return sorted(rng.randrange(lo, hi) for _ in range(count))


def read_and_unread(book: Book, reader_para: int) -> tuple[str, str]:
    read = "\n\n".join(p.text for p in book.paragraphs[: reader_para + 1])
    unread = "\n\n".join(p.text for p in book.paragraphs[reader_para + 1:])
    return read, unread


def structural_leak(passages: list[dict], reader_para: int) -> bool:
    """Deterministic check: a passage ending after the reader's paragraph is a leak, whatever the LLM says."""
    return any(p["para_end"] > reader_para for p in passages)


CallFn = Callable[..., Awaitable[BaseModel]]
RetrieveFn = Callable[[str, int, str], dict]


async def generate_cases(book: Book, positions: list[int], call: CallFn) -> list[Case]:
    async def one(pos: int) -> list[Case]:
        read, unread = read_and_unread(book, pos)
        user = f'Story: "{book.title}"\n\n### Read text\n{read}\n\n### Unread text\n{unread}'
        batch = await call(GEN_MODEL, GEN_SYSTEM, user, QuestionBatch, max_tokens=1500, effort="low")
        return [Case(question=q, reader_para=pos, speaker="narrator") for q in batch.questions]

    return [c for group in await asyncio.gather(*(one(p) for p in positions)) for c in group]


def pick_speakers(book: Book, cases: list[Case], seed: int = 11) -> None:
    """Alternate between the narrator and any character already met at that position."""
    rng = random.Random(seed)
    for c in cases:
        met = [ch.id for ch in book.characters if ch.first_para <= c.reader_para]
        c.speaker = rng.choice(["narrator", *met])


async def run_case(book: Book, case: Case, retrieve: RetrieveFn, call: CallFn) -> Case:
    out = retrieve(case.question.question, case.reader_para, case.speaker)
    case.passages, case.instructions = out["passages"], out["instructions"]
    case.structural_leak = structural_leak(case.passages, case.reader_para)
    case.answer = (await call(ANSWER_MODEL, case.instructions, case.question.question, Answer, max_tokens=400, effort="low")).answer
    read, unread = read_and_unread(book, case.reader_para)
    user = (f"### Read text\n{read[-6000:]}\n\n### Unread text\n{unread[:12000]}\n\n"
            f"### Question ({case.question.kind})\n{case.question.question}\nKey fact: {case.question.key_fact}\n\n"
            f"### Answer to judge\n{case.answer}")
    case.verdict = await call(JUDGE_MODEL, JUDGE_SYSTEM, user, Verdict, max_tokens=300, effort="low")
    return case


def summarize(cases: list[Case]) -> dict:
    judged = [c for c in cases if c.verdict]
    earlier = [c for c in judged if c.question.kind == "earlier"]
    return {
        "questions": len(cases),
        "llm_leaks": sum(c.verdict.leaked_future_info for c in judged),
        "structural_leaks": sum(c.structural_leak for c in cases),
        "earlier_accuracy": (sum(c.verdict.answered_correctly for c in earlier) / len(earlier)) if earlier else None,
        "future_handled": (sum(c.verdict.answered_correctly for c in judged if c.question.kind == "future")
                           / max(1, sum(c.question.kind == "future" for c in judged))),
    }


def render_report(book: Book, cases: list[Case], stats: dict) -> str:
    acc = stats["earlier_accuracy"]
    lines = [
        f"## Spoiler eval, {book.title} ({date.today().isoformat()})",
        "",
        f"- Questions: {stats['questions']} at {len({c.reader_para for c in cases})} reader positions",
        f"- **Leaks (Opus judge): {stats['llm_leaks']}**; passages beyond the reader's paragraph: {stats['structural_leaks']}",
        f"- Earlier-text accuracy: {'n/a' if acc is None else f'{acc:.0%}'}; future questions declined or stayed in bounds: {stats['future_handled']:.0%}",
        f"- Models: questions {GEN_MODEL}, answers {ANSWER_MODEL} (proxy for Higgs Realtime), judge {JUDGE_MODEL}",
        "",
    ]
    bad = [c for c in cases if c.structural_leak or (c.verdict and c.verdict.leaked_future_info)]
    if bad:
        lines.append("### Leaks to review")
        for c in bad:
            lines += [f"- ¶{c.reader_para + 1}, {c.speaker}: {c.question.question}",
                      f"  - answer: {c.answer}", f"  - judge: {c.verdict.reason if c.verdict else 'structural'}"]
    else:
        lines.append("No leaks.")
    return "\n".join(lines) + "\n"


def estimate(book: Book, positions: int) -> float:
    import llm

    text_chars = sum(len(p.text) for p in book.paragraphs)
    n = positions * PER_POSITION
    gen = llm.estimate_cost(GEN_MODEL, text_chars + 800, 300, calls=positions)
    answer = llm.estimate_cost(ANSWER_MODEL, 6000, 120, calls=n)
    judge = llm.estimate_cost(JUDGE_MODEL, 9000 + 2000, 80, calls=n)
    return gen + answer + judge


def api_retrieve(book_id: str) -> RetrieveFn:
    """The real /api/retrieve endpoint, called in-process."""
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)

    def call(query: str, reader_para: int, speaker: str) -> dict:
        r = client.post("/api/retrieve", json={"book_id": book_id, "query": query, "reader_para": reader_para, "speaker_id": speaker})
        r.raise_for_status()
        return r.json()

    return call


async def main() -> None:
    import llm

    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True)
    ap.add_argument("--n", type=int, default=100, help="total questions (4 per reader position)")
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the cost estimate and stop")
    ap.add_argument("--notes", action="store_true", help="append the summary to NOTES.md")
    args = ap.parse_args()
    book = Book.model_validate_json((ROOT / "data/books" / args.book / "book.json").read_text(encoding="utf-8"))
    if book.kind != "novel":
        sys.exit("The spoiler rule applies to novels; textbooks may use the whole chapter.")
    if not book.summaries:
        print("warning: no summaries yet (run summaries.py); the eval will still run on passages alone")

    positions = sample_positions(book, max(1, args.n // PER_POSITION))
    est = estimate(book, len(positions))
    print(f"eval_spoilers: {len(positions) * PER_POSITION} questions, estimated ${est:.2f}")
    if args.dry_run:
        return
    if est > 5 and not args.yes:
        sys.exit("estimate > $5; rerun with --yes")

    retrieve = api_retrieve(args.book)
    cases = await generate_cases(book, positions, llm.call_structured)
    pick_speakers(book, cases)
    sem = asyncio.Semaphore(6)

    async def guarded(c: Case) -> Case:
        async with sem:
            return await run_case(book, c, lambda q, p, s: retrieve(q, p, s), llm.call_structured)

    cases = await asyncio.gather(*(guarded(c) for c in cases))
    stats = summarize(cases)
    report = render_report(book, cases, stats)
    out = ROOT / "data/eval" / f"{args.book}-spoilers.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(report)
    if args.notes:
        with (ROOT / "NOTES.md").open("a", encoding="utf-8") as f:
            f.write("\n" + report)
    llm.print_usage()
    sys.exit(1 if stats["llm_leaks"] or stats["structural_leaks"] else 0)


if __name__ == "__main__":
    asyncio.run(main())
