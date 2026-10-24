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
        return Verdict(leaked_future_info="end" in user and "SPOILER" in user, answered_correctly=True, detail=2, reason="ok")

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


def test_pick_speakers_skips_non_main_characters():
    book = make_book()
    for ch in book.characters:
        ch.main = False
    book.characters[0].main = None
    cases = [Case(question=Question(question="q", kind="earlier", key_fact="k"), reader_para=999, speaker="narrator") for _ in range(40)]
    pick_speakers(book, cases)
    allowed = {"narrator", book.characters[0].id}
    assert {c.speaker for c in cases} <= allowed


def test_report_lists_a_flagged_leak():
    book = make_book()
    leaky = Case(question=Question(question="Who did it?", kind="future", key_fact="X"), reader_para=5, speaker="narrator",
                 answer="It was X.", verdict=Verdict(leaked_future_info=True, answered_correctly=False, detail=2, reason="named X"))
    stats = summarize([leaky])
    assert stats["llm_leaks"] == 1
    report = render_report(book, [leaky], stats)
    assert "Leaks to review" in report and "Who did it?" in report and "named X" in report


def test_earlier_book_passages_are_not_structural_leaks():
    from eval_spoilers import structural_leak
    assert not structural_leak([{"para_start": 500, "para_end": 900, "text": "x", "book": "Book 1"}], 10)
    assert structural_leak([{"para_start": 5, "para_end": 11, "text": "x"}], 10)


def test_question_context_is_bounded_and_includes_the_ending():
    from eval_spoilers import ENDING_CHARS, READ_WINDOW, UNREAD_WINDOW, question_context
    from app.schema import Book, Paragraph, Section
    paras = [Paragraph(idx=i, section_id="s1", text=f"p{i} " + "x" * 995) for i in range(200)]
    paras[-1].text = "THE ENDING " + "y" * 900
    book = Book(id="b", title="T", author="A", kind="novel", license="x", sections=[Section(id="s1", title="", start_para=0)],
                paragraphs=paras)
    read, unread = question_context(book, 30)
    assert len(read) <= READ_WINDOW and "p30 " in read and "p31 " not in read
    assert len(unread) <= UNREAD_WINDOW + ENDING_CHARS + 20 and "p31 " in unread and "THE ENDING" in unread


def test_judge_prompts_differ_by_mode_and_full_mode_gives_future_ground_truth():
    from eval_spoilers import FULL_JUDGE_SYSTEM, JUDGE_SYSTEM, build_judge_prompt
    book = make_book()
    fut = Case(question=Question(question="How does it end?", kind="future", key_fact="KEYFACT-END"), reader_para=10,
               speaker="narrator", answer="It ends well.")
    sys_safe, user_safe = build_judge_prompt(book, fut, "safe")
    sys_full, user_full = build_judge_prompt(book, fut, "full")
    assert sys_safe == JUDGE_SYSTEM and sys_full == FULL_JUDGE_SYSTEM
    assert "### Later text (not read yet)" in user_safe and "Later text (excerpt)" in user_full
    assert "marker30" in user_full and "KEYFACT-END" in user_full and "It ends well." in user_full
    earlier = Case(question=Question(question="q", kind="earlier", key_fact="K"), reader_para=10, speaker="narrator", answer="a")
    assert "Later text" not in build_judge_prompt(book, earlier, "full")[1]


def test_full_mode_plumbing_scores_future_accuracy_detail_and_beyond_passages():
    from eval_spoilers import FullVerdict, mode_list
    assert mode_list("both") == ("safe", "full") and mode_list("full") == ("full",)
    book = make_book()
    calls = []

    async def fake(model, system, user, schema, **kw):
        calls.append((model, schema))
        if schema is Answer:
            return Answer(answer="It ends.")
        return FullVerdict(answered_correctly="KEY" in user, detail=3, reason="ok")

    def retrieve(q, p, s):  # full-mode style: a passage past the reader
        return {"passages": [{"para_start": p, "para_end": p + 5, "text": "x"}], "instructions": "i"}

    cases = [Case(question=Question(question="q", kind=k, key_fact="KEY"), reader_para=10, speaker="narrator")
             for k in ("earlier", "future")]
    done = [run(run_case(book, c, retrieve, fake, None, "full", "judge-x")) for c in cases]
    stats = summarize(done, "full")
    assert stats["earlier_accuracy"] == 1.0 and stats["future_handled"] == 1.0
    assert stats["avg_detail_earlier"] == 3 and stats["avg_detail_future"] == 3
    assert stats["structural_leaks"] == 2 and stats["llm_leaks"] == 0
    assert ("judge-x", FullVerdict) in calls
    report = render_report(book, done, stats)
    assert "expected > 0" in report and "future-question accuracy: 100%" in report


def test_safe_mode_detail_and_default_judge_is_sonnet():
    from eval_spoilers import JUDGE_MODEL
    assert JUDGE_MODEL == "claude-sonnet-5-5"
    c = Case(question=Question(question="q", kind="earlier", key_fact="k"), reader_para=5, speaker="narrator",
             verdict=Verdict(leaked_future_info=False, answered_correctly=True, detail=1, reason="r"))
    assert summarize([c])["avg_detail_earlier"] == 1


def test_judge_context_has_summary_recap_short_read_tail_and_later_text():
    from eval_spoilers import LATER_FULL_END, LATER_FULL_NEAR, LATER_SAFE, READ_TAIL, build_judge_prompt, story_so_far
    from app.schema import Summary
    paras = [Paragraph(idx=i, section_id="s1", text=f"p{i} " + "x" * 996) for i in range(100)]
    book = Book(id="b", title="T", author="A", kind="novel", license="x", sections=[Section(id="s1", title="", start_para=0)],
                paragraphs=paras, summaries=[Summary(upto_para=20, text="EARLY-SUMMARY"), Summary(upto_para=80, text="LATE-SUMMARY")])
    assert "EARLY-SUMMARY" in story_so_far(book, 30) and "LATE-SUMMARY" not in story_so_far(book, 30)
    q = Question(question="q?", kind="future", key_fact="KEY")
    case = Case(question=q, reader_para=30, speaker="narrator", answer="ANS",
                passages=[{"para_start": 1, "para_end": 2, "text": "PASSAGE-TEXT"}])
    _, safe = build_judge_prompt(book, case, "safe")
    assert "EARLY-SUMMARY" in safe and "LATE-SUMMARY" not in safe and "KEY" in safe and "ANS" in safe
    assert "PASSAGE-TEXT" not in safe  # logged only, not shown, so before/after use the same judge context
    assert len(safe) < 2000 + READ_TAIL + LATER_SAFE + 200
    assert "p31 " in safe and "p30 " in safe and "p20 " not in safe  # tail starts well after p20 (4k chars)
    _, full = build_judge_prompt(book, case, "full")
    assert len(full) < 2000 + READ_TAIL + LATER_FULL_NEAR + LATER_FULL_END + 400
    assert "p99 " in full  # ending
    assert "PASSAGE-TEXT" in build_judge_prompt(book, case, "safe", include_passages=True)[1]


def test_rejudge_loads_saved_answers_and_judges_without_answering(tmp_path):
    import json
    from eval_spoilers import judge_case, load_saved_cases, results_json
    book = make_book()
    done = Case(question=Question(question="q", kind="earlier", key_fact="k"), reader_para=10, speaker="narrator",
                answer="saved answer", structural_leak=False,
                passages=[{"para_start": 1, "para_end": 2, "text": "T", "book": None}],
                verdict=Verdict(leaked_future_info=True, answered_correctly=False, detail=1, reason="old"))
    (tmp_path / "b-safe.json").write_text(results_json([done], {}), encoding="utf-8")
    assert json.loads((tmp_path / "b-safe.json").read_text())["cases"][0]["passages"][0]["text"] == "T"  # logged for inspection
    cases = load_saved_cases(tmp_path, "b", "safe")
    assert len(cases) == 1 and cases[0].answer == "saved answer" and cases[0].verdict is None
    seen = []

    async def fake(model, system, user, schema, **kw):
        seen.append((schema, user))
        return Verdict(leaked_future_info=False, answered_correctly=True, detail=3, reason="new")

    out = run(judge_case(book, cases[0], fake, "safe", "judge-x"))
    assert out.verdict.reason == "new" and len(seen) == 1 and "saved answer" in seen[0][1]


def test_estimate_counts_real_prompt_chars_including_later_text():
    from eval_spoilers import estimate
    book = make_book()
    cases = [Case(question=Question(question="q", kind=k, key_fact="K"), reader_para=10, speaker="narrator") for k in ("earlier", "future")]
    usd_safe, chars_safe = estimate(book, cases, ("safe",))
    usd_both, chars_both = estimate(book, cases, ("safe", "full"))
    assert chars_safe > 0 and chars_both > chars_safe and usd_both > usd_safe > 0
