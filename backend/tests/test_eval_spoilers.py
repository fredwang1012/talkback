"""eval_spoilers with fake LLM calls: no network, no credits."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocess"))
from eval_spoilers import (  # noqa: E402
    Answer, Case, Question, QuestionBatch, Verdict, generate_cases, pick_speakers, read_and_unread,
    render_report, run_case, sample_positions, structural_leak, summarize,
)

from app import retrieval  # noqa: E402
from app.personas import build_instructions  # noqa: E402
from app.schema import Book, Character, Narrator, Paragraph, Section  # noqa: E402


def make_book(n=60) -> Book:
    paras = [Paragraph(idx=i, section_id="s1", text=f"Paragraph {i} mentions marker{i} and the clue{i} of the case.") for i in range(n)]
    return Book(id="b", title="T", author="A", kind="novel", license="x",
                sections=[Section(id="s1", title="I.", start_para=0)], paragraphs=paras,
                characters=[Character(id="harry", name="Harry", aliases=["Harry"], first_para=20, voice="oliver", persona="Terse.")],
                narrator=Narrator(voice="marcus"))


def retrieve_for(book):
    index = retrieval.Index(retrieval.make_chunks(book))

    def fn(query, reader_para, speaker):
        out = retrieval.retrieve(book, index, query, reader_para, 4)
        out["instructions"] = build_instructions(book, speaker, out["story_so_far"], out["passages"])
        return out

    return fn


def run(coro):
    return asyncio.run(coro)


def test_positions_are_seeded_inside_the_book_with_room_on_both_sides():
    book = make_book()
    a, b = sample_positions(book, 25), sample_positions(book, 25)
    assert a == b and a == sorted(a)
    assert all(5 <= p <= 50 for p in a)


def test_read_and_unread_split_exactly_at_the_reader():
    book = make_book(10)
    read, unread = read_and_unread(book, 3)
    assert "marker3" in read and "marker4" not in read
    assert "marker4" in unread and "marker3" not in unread


def test_structural_leak_flags_any_passage_past_the_reader():
    assert not structural_leak([{"para_end": 5}, {"para_end": 7}], 7)
    assert structural_leak([{"para_end": 5}, {"para_end": 8}], 7)


def test_real_retrieval_never_trips_the_structural_check_at_any_position():
    book, fn = make_book(), None
    fn = retrieve_for(book)
    for pos in range(0, 60, 7):
        for q in ("How does it end?", "what is clue59", "who is Harry"):
            assert not structural_leak(fn(q, pos, "narrator")["passages"], pos), (pos, q)


def test_full_flow_with_fake_models_counts_leaks_and_accuracy():
    book = make_book()

    async def fake(model, system, user, schema, **kw):
        if schema is QuestionBatch:
            return QuestionBatch(questions=[
                Question(question="What is clue1?", kind="earlier", key_fact="a clue"),
                Question(question="How does it end?", kind="future", key_fact="the end")])
        if schema is Answer:
            return Answer(answer="He cannot say yet." if "end" in user else "Clue one.")
        return Verdict(leaked_future_info="end" in user and "SPOILER" in user, answered_correctly=True, reason="ok")

    cases = run(generate_cases(book, [10, 30], fake))
    assert len(cases) == 4
    pick_speakers(book, cases)
    assert all(c.speaker == "narrator" for c in cases if c.reader_para < 20)  # Harry not met yet
    retrieve = retrieve_for(book)
    done = [run(run_case(book, c, retrieve, fake)) for c in cases]
    stats = summarize(done)
    assert stats["questions"] == 4 and stats["llm_leaks"] == 0 and stats["structural_leaks"] == 0
    assert stats["earlier_accuracy"] == 1.0
    assert "No leaks." in render_report(book, done, stats)


def test_report_lists_a_flagged_leak():
    book = make_book()
    leaky = Case(question=Question(question="Who did it?", kind="future", key_fact="X"), reader_para=5, speaker="narrator",
                 answer="It was X.", verdict=Verdict(leaked_future_info=True, answered_correctly=False, reason="named X"))
    stats = summarize([leaky])
    assert stats["llm_leaks"] == 1
    report = render_report(book, [leaky], stats)
    assert "Leaks to review" in report and "Who did it?" in report and "named X" in report
