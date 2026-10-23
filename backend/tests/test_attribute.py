"""Heuristic attribution of untagged dialogue (app/enrich/attribute.py)."""
import asyncio

from app.enrich import attribute as at
from app.schema import Book, Character, Paragraph, Section, Segment


def N(text, speaker="narrator"):
    return Segment(speaker=speaker, text=text)


def book_of(paras: list[list[Segment]]) -> Book:
    chars = [Character(id="harry", name="Harry", aliases=["Harry", "Harry Potter"], first_para=0, voice="oliver", persona="p"),
             Character(id="ron", name="Ron", aliases=["Ron"], first_para=0, voice="jake", persona="p"),
             Character(id="hermione", name="Hermione", aliases=["Hermione"], first_para=0, voice="nora", persona="p")]
    return Book(id="t", title="T", author="a", kind="novel", license="x", characters=chars,
                sections=[Section(id="s", title="S", start_para=0)],
                paragraphs=[Paragraph(idx=i, section_id="s", text=" ".join(s.text for s in segs), segments=segs)
                            for i, segs in enumerate(paras)])


def speakers(book, i):
    return [(s.speaker, s.text) for s in book.paragraphs[i].segments]


def test_alternation_in_a_two_person_exchange():
    b = book_of([
        [N('"Where were you?"', "harry"), N("asked Harry.")],
        [N('"In the library,"', "ron"), N("said Ron.")],
        [N('"Doing what?"')],
        [N('"Looking for the book we need."')],
        [N('"Did you find it?"')],
    ])
    ds = at.attribute_book(b)
    assert [d.speaker for d in ds] == ["harry", "ron", "harry"]
    assert ds[0].rule == "alternation" and ds[0].confidence == "medium"   # only A,B seen so far
    assert ds[1].confidence == "high"                                      # A,B,A pattern
    assert speakers(b, 3) == [("ron", '"Looking for the book we need."')]


def test_vocative_vetoes_the_alternation_pick():
    b = book_of([
        [N('"Hi,"', "harry"), N("said Harry.")],
        [N('"Hello,"', "ron"), N("said Ron.")],
        [N('"Ron, did you eat?"')],       # alternation says Harry; speaker is addressing Ron, so Harry is fine
        [N('"Ron, are you listening?"')],  # alternation says Ron, who is being addressed: unsure
    ])
    ds = at.attribute_book(b)
    assert ds[0].speaker == "harry"
    assert ds[1].speaker is None and ds[1].confidence == "ambiguous"
    assert speakers(b, 3) == [("narrator", '"Ron, are you listening?"')]


def test_tag_in_a_narrator_segment_splits_it_and_names_the_speaker():
    b = book_of([[N('"Run!" Hermione screamed, and everyone ran.')]])
    ds = at.attribute_book(b)
    assert ds[0].speaker == "hermione" and ds[0].rule == "tag"
    assert speakers(b, 0) == [("hermione", '"Run!"'), ("narrator", "Hermione screamed, and everyone ran.")]


def test_tag_by_someone_outside_the_cast_stays_with_the_narrator():
    b = book_of([[N('"Gerroff me!" squealed the gnome.')]])
    ds = at.attribute_book(b)
    assert ds[0].speaker is None and ds[0].confidence == "uncast"
    assert len(b.paragraphs[0].segments) == 1


def test_action_beat_before_the_quote():
    b = book_of([[N('Ron grinned at the others. "This is going to be brilliant."')]])
    ds = at.attribute_book(b)
    assert ds[0].speaker == "ron" and ds[0].rule == "action"
    assert speakers(b, 0)[1] == ("ron", '"This is going to be brilliant."')


def test_continuity_after_an_action_beat_in_the_same_paragraph():
    b = book_of([[N('"Wait,"', "harry"), N("he said, and looked back."), N('"I heard something outside the door."')]])
    ds = at.attribute_book(b)
    assert ds and ds[0].speaker == "harry" and ds[0].rule == "continuity"


def test_continuity_not_applied_when_another_character_acts_in_between():
    b = book_of([[N('"Wait,"', "harry"), N("he said. Ron frowned."), N('"I heard something outside the door."')]])
    ds = at.attribute_book(b)
    assert ds[0].speaker == "ron" or ds[0].speaker is None
    assert ds[0].speaker != "harry"


def test_single_exchange_without_history_is_ambiguous_and_one_word_quotes_are_ignored():
    b = book_of([[N('The sign said "Closed" in red.')], [N('"Come in, come in!"')]])
    ds = at.attribute_book(b)
    assert [(d.text, d.speaker) for d in ds] == [('"Come in, come in!"', None)]
    assert b.paragraphs[1].segments[0].speaker == "narrator"


def test_exchange_resets_after_a_long_narration_gap():
    b = book_of([
        [N('"One,"', "harry"), N("said Harry.")],
        [N('"Two,"', "ron"), N("said Ron.")],
        [N("Nothing happened for a long while.")],
        [N("Then the sun rose over the hills.")],
        [N('"Three, I suppose."')],
    ])
    assert at.attribute_book(b)[0].speaker is None


def test_segments_still_tile_the_paragraph_and_apply_false_changes_nothing():
    texts = [[N('"Hi,"', "harry"), N("said Harry.")], [N('"Hello," Ron said. "Nice to see you again."')]]
    b = book_of(texts)
    before = b.model_dump()
    at.attribute_book(b, apply=False)
    assert b.model_dump() == before
    at.attribute_book(b)
    joined = " ".join(s.text for s in b.paragraphs[1].segments)
    assert joined.replace("  ", " ") == b.paragraphs[1].text


def test_summarize_counts():
    b = book_of([[N('"Hi,"', "harry"), N("said Harry.")], [N('"Hello," Ron said.')], [N('"Ok then, bye."')]])
    s = at.summarize(at.attribute_book(b))
    assert s["lines"] == 2 and s["reassigned"] >= 1


def test_llm_pass_applies_only_known_ids_and_estimate_is_positive():
    b = book_of([[N('"Come in, come in!"')], [N('"Thanks for having us."')]])
    ds = at.attribute_book(b)
    assert all(d.speaker is None for d in ds)
    est = at.llm_estimate([(b, ds)])
    assert est["calls"] == 1 and est["usd"] > 0

    async def call(model, system, user, schema, **kw):
        return at.LineAnswers(lines=[at.LineAnswer(n=0, speaker="hermione"), at.LineAnswer(n=1, speaker="dumbledore")])
    assert asyncio.run(at.llm_attribute(b, ds, call)) == 1
    assert b.paragraphs[0].segments[0].speaker == "hermione"
    assert b.paragraphs[1].segments[0].speaker == "narrator"


def test_unlisted_speech_verb_with_a_cast_name_is_a_tag():
    b = book_of([[N('"That is not the point!" raged Harry. "You wait here."')]])
    ds = at.attribute_book(b)
    assert ds[0].speaker == "harry" and ds[0].rule == "tag"


def test_tag_before_uses_the_name_at_the_final_verb_not_an_earlier_name():
    b = book_of([[N('"What?" asked Ron, but a voice behind them said, "Hello there."')]])
    ds = at.attribute_book(b)
    assert [d.speaker for d in ds] == [None, None] or ds[1].speaker != "ron"
    assert ds[1].confidence in ("uncast", "ambiguous")


def test_perception_sentence_and_earlier_quote_do_not_make_an_action_attribution():
    b = book_of([[N('"Ah, okay," said Cedric. Harry could tell he did not believe him. "Well, see you then."')]])
    ds = at.attribute_book(b)
    assert all(d.speaker != "harry" for d in ds)


def test_pronoun_gender_vetoes_a_wrong_alternation_pick():
    # Ron is always "he", Hermione always "she"
    paras = []
    for i in range(4):
        paras.append([N('"Right then, mate," ', "ron"), N("he said.")])
        paras.append([N('"Quite so," ', "hermione"), N("she said.")])
    paras += [[N('"Right then, mate," ', "ron"), N("he said.")], [N('"Quite so," ', "hermione"), N("she said.")],
              [N('"Shall we go now?" she asked.')]]
    b = book_of(paras)
    ds = at.attribute_book(b)
    assert ds[-1].speaker != "ron"
    assert at.infer_genders(b) == {"ron": "he", "hermione": "she"}
