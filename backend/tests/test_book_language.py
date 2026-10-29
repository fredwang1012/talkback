"""Uploads in other languages (#117): the book's language is detected at ingest and drives sentence splitting, TTS,
chapters, search and the prompts. English books must behave exactly as before. No test calls Boson or an LLM."""
import asyncio
import json

import httpx
import pytest

from app import booklang, main, multilingual, personas, retrieval, tts_cache
from app.boson import BosonClient
from app.enrich import cast as cast_mod
from app.enrich import detect as detect_mod
from app.enrich import pipeline
from app.enrich import segment as seg_mod
from app.enrich import summaries as sum_mod
from app.enrich import textbook as tb_mod
from app.ingest_upload import HEADING_REGEX, Line, build_book, merge_lines
from app.parsing import normalize
from app.public_api import AnswerRequest, EnrichRequest
from app.public_client import _payload, answer_payload
from app.schema import Book, Character, Narrator, Paragraph, Section, Segment, TtsChunk
from app.ttschunks import CJK_MAX_CHARS, MAX_CHARS, chunk_paragraph, rechunk_book, sentence_spans

from .test_api import env  # noqa: F401  (env fixture)

SPANISH = ("En un lugar de la Mancha, de cuyo nombre no quiero acordarme, no ha mucho tiempo que vivía un hidalgo de "
           "los de lanza en astillero, adarga antigua, rocín flaco y galgo corredor. Una olla de algo más vaca que "
           "carnero, salpicón las más noches, duelos y quebrantos los sábados, lantejas los viernes.")
FRENCH = ("Longtemps, je me suis couché de bonne heure. Parfois, à peine ma bougie éteinte, mes yeux se fermaient si "
          "vite que je n'avais pas le temps de me dire que je m'endormais. Et, une demi-heure après, la pensée qu'il "
          "était temps de chercher le sommeil m'éveillait.")
GERMAN = ("Als Gregor Samsa eines Morgens aus unruhigen Träumen erwachte, fand er sich in seinem Bett zu einem "
          "ungeheueren Ungeziefer verwandelt. Er lag auf seinem panzerartig harten Rücken und sah, wenn er den Kopf "
          "ein wenig hob, seinen gewölbten braunen Bauch.")
CHINESE = "话说天下大势，分久必合，合久必分。周末七国分争，并入于秦。及秦灭之后，楚、汉分争，又并入于汉。"
JAPANESE = "吾輩は猫である。名前はまだ無い。どこで生れたかとんと見当がつかぬ。何でも薄暗いじめじめした所で泣いていた。"
ENGLISH = ("Alice was beginning to get very tired of sitting by her sister on the bank, and of having nothing to do: "
           "once or twice she had peeped into the book her sister was reading, but it had no pictures in it.")
TEXTBOOK_EN = ("In this chapter we define the inverted index and show how a search engine uses it. Definition 2.1: a "
               "posting list is the list of documents in which a term occurs. Figure 2.3 shows an example of the "
               "dictionary and the postings, and Table 2.1 lists their sizes for the Reuters collection.")


# ---- detection ----

@pytest.mark.parametrize("text, code", [
    (SPANISH, "spa"), (FRENCH, "fra"), (GERMAN, "deu"), (CHINESE, "zho"), (JAPANESE, "jpn"),
    ("Все счастливые семьи похожи друг на друга, каждая несчастливая семья несчастлива по-своему.", "rus"),
    (ENGLISH, "eng"), (TEXTBOOK_EN, "eng"),
    ("Hagrid", "eng"),                     # too little to go on
    ("", "eng"),
])
def test_detect(text, code):
    assert booklang.detect(text) == code


def test_an_english_book_with_foreign_names_and_phrases_stays_english():
    text = ENGLISH + " 'Señor Don Quixote de la Mancha,' she read, 'et le roi de France.' " + ENGLISH
    assert booklang.detect(text) == "eng"


def test_book_language_defaults_and_unknown_codes_are_english():
    base = dict(id="b", title="T", author="A", kind="novel", license="x", sections=[], paragraphs=[])
    assert Book(**base).language == "eng"
    assert Book(**base, language="spa").language == "spa"
    assert Book(**base, language="xx!").language == "eng"   # Boson would refuse every request


# ---- sentences, quotes, dialogue ----

def spans(text, lang):
    return [text[a:b] for a, b in sentence_spans(text, lang)]


def test_spanish_dialogue_dash_and_abbreviations():
    text = "—¿Vienes? —preguntó él. —No. El Sr. García llegó tarde."
    assert spans(text, "spa") == ["—¿Vienes? —preguntó él.", "—No.", "El Sr. García llegó tarde."]


def test_french_spaced_punctuation_and_guillemets():
    text = "« Bonjour ! » dit-il. Il partit. M. Dupont resta."
    assert spans(text, "fra") == ["« Bonjour ! » dit-il.", "Il partit.", "M. Dupont resta."]


def test_chinese_and_japanese_sentence_ends_need_no_space():
    assert spans("他说：「你好。」然后走了。她笑了！", "zho") == ["他说：「你好。」", "然后走了。", "她笑了！"]
    assert spans(JAPANESE, "jpn")[:2] == ["吾輩は猫である。", "名前はまだ無い。"]


def test_english_sentences_are_unchanged():
    text = 'Mr. Darling said, "Well?" said he. It was 1888. Then e.g. Wendy left.'
    assert sentence_spans(text, "eng") == sentence_spans(text) == sentence_spans(text, None)


def test_has_dialogue():
    assert booklang.has_dialogue('He said "hi".', "eng")
    assert not booklang.has_dialogue("—Hola —dijo.", "eng")             # English: the old test only
    assert booklang.has_dialogue("—Hola —dijo él.", "spa")
    assert booklang.has_dialogue("« Bonjour », dit-il.", "fra")
    assert booklang.has_dialogue("„Guten Tag“, sagte er.", "deu")
    assert booklang.has_dialogue("他说：「你好。」", "zho")
    assert not booklang.has_dialogue("Era una noche oscura.", "spa")


# ---- TTS chunks ----

def book_in(lang, paragraphs, characters=()):
    return Book(id="b", title="T", author="A", kind="novel", license="x", language=lang,
                sections=[Section(id="s1", title="", start_para=0)],
                paragraphs=[Paragraph(idx=i, section_id="s1", text=t) for i, t in enumerate(paragraphs)],
                characters=list(characters), narrator=Narrator(voice="marcus"))


def test_a_spanish_book_is_read_as_written_without_english_rules():
    ana = Character(id="ana", name="Ana", first_para=0, voice="nora", persona="x")
    text = '"Ha ha, tengo 3 gatos", whispered Ana.'
    book = book_in("spa", [text], [ana])
    p = book.paragraphs[0]
    p.segments = [Segment(speaker="ana", text='"Ha ha, tengo 3 gatos",'), Segment(speaker="narrator", text="whispered Ana.")]
    chunks = chunk_paragraph(p, book)
    joined = "".join(c.tts_text for c in chunks)
    assert "tengo 3 gatos" in joined          # numbers are left to Boson's normalization in Spanish
    assert "three" not in joined
    assert "<|sfx" not in joined and "whispering" not in joined   # no English laughter / speech-tag rules
    english = book_in("eng", [text], [ana])
    english.paragraphs[0].segments = list(p.segments)
    e = "".join(c.tts_text for c in chunk_paragraph(english.paragraphs[0], english))
    assert "three" in e and "whispering" in e   # the English rules still apply to an English book


def test_the_llm_style_is_kept_in_another_language():
    ana = Character(id="ana", name="Ana", first_para=0, voice="nora", persona="x")
    book = book_in("spa", ["—¡Corre! —gritó Ana."], [ana])
    p = book.paragraphs[0]
    p.segments = [Segment(speaker="ana", text="—¡Corre!", style="shouting"),
                  Segment(speaker="narrator", text="—gritó Ana.")]
    assert "<|style:shouting|>" in chunk_paragraph(p, book)[0].tts_text


def test_chinese_chunks_are_shorter_and_split_at_sentence_ends():
    text = CHINESE * 6
    book = book_in("zho", [text])
    rechunk_book(book)
    chunks = book.paragraphs[0].tts_chunks
    assert len(chunks) > 1 and all(c.char_end - c.char_start <= CJK_MAX_CHARS for c in chunks)
    assert all(text[c.char_end - 1] in "。！？" for c in chunks[:-1])   # whole sentences
    assert CJK_MAX_CHARS < MAX_CHARS


def test_textbook_notes_are_said_in_the_book_language():
    book = book_in("spa", ["for x in y: pass", "y = 2x"])
    book.kind = "textbook"
    book.paragraphs[0].kind = "code"
    assert chunk_paragraph(book.paragraphs[0], book)[0].tts_text == "Se omite un listado de código."


# ---- ingest: chapters, CJK lines, language stored ----

def test_build_book_detects_spanish_and_finds_its_chapters():
    raw = f"Capítulo 1\n\n{SPANISH}\n\nCapítulo dos\n\n{SPANISH}\n\nCAPÍTULO III\n\n{SPANISH}"
    book = build_book("x", "T", "A", "novel", raw)
    assert book.language == "spa"
    assert [s.title for s in book.sections] == ["Capítulo 1", "Capítulo dos", "CAPÍTULO III"]


def test_build_book_chinese_chapters_and_no_spaces_between_lines():
    raw = f"第一章 宴桃园豪杰三结义\n\n{CHINESE[:20]}\n{CHINESE[20:]}\n\n第二回\n\n{CHINESE}"
    book = build_book("x", "T", "A", "novel", raw)
    assert book.language == "zho"
    assert [s.title for s in book.sections] == ["第一章 宴桃园豪杰三结义", "第二回"]
    assert book.paragraphs[0].text == CHINESE   # the line break inside the paragraph is no word break


def test_heading_regex_other_languages():
    import re
    h = re.compile(HEADING_REGEX)
    for title in ("Chapitre 3", "Kapitel 12", "Capitolo primo", "Глава 5", "第十二章", "제3장"):
        assert h.match(title), title
    assert not h.match("Capítulo 3, en el que pasa algo.")   # same comma rule as "Chapter 3, ..."


def test_merge_lines_joins_cjk_without_a_space():
    def line(t, y):
        return Line(text=t, x0=10, x1=500, y0=y, y1=y + 10, page=0, left=10, right=500, alone=False, drop=False)
    assert merge_lines([line("他说今天", 0), line("天气很好。", 11)]) == ["他说今天天气很好。"]
    assert merge_lines([line("It was a", 0), line("dark night.", 11)]) == ["It was a dark night."]


def test_normalize_joins_only_line_breaks_between_cjk():
    assert normalize("他说 今天\n天气") == "他说 今天天气"     # a written space stays (a title after 第一章)
    assert normalize("It  was\na night 2\n天") == "It was a night 2 天"


def test_explicit_language_wins_over_detection():
    assert build_book("x", "T", "A", "novel", SPANISH, language="eng").language == "eng"


def _pdf(pages, fontname="helv"):
    import pymupdf as fitz
    doc = fitz.open()
    for heading, body in pages:
        page = doc.new_page()
        page.insert_text((72, 90), heading, fontsize=20, fontname=fontname)
        page.insert_textbox(fitz.Rect(72, 130, 500, 700), body, fontsize=11, fontname=fontname)
    return doc.tobytes()


def test_a_spanish_pdf_end_to_end():
    from app.ingest_upload import extract_text
    data = _pdf([("Capítulo 1", SPANISH + " " + SPANISH), ("Capítulo 2", SPANISH)])
    book = build_book("x", "T", "A", "novel", extract_text(data, ".pdf"))
    assert book.language == "spa"
    assert [s.title for s in book.sections] == ["Capítulo 1", "Capítulo 2"]
    assert book.paragraphs[0].text.startswith("En un lugar de la Mancha")


def test_a_chinese_pdf_end_to_end():
    from app.ingest_upload import extract_text
    data = _pdf([("第一章", CHINESE * 8), ("第二章", CHINESE * 3)], fontname="china-s")
    book = build_book("x", "T", "A", "novel", extract_text(data, ".pdf"))
    assert book.language == "zho"
    assert [s.title for s in book.sections] == ["第一章", "第二章"]
    assert " " not in book.paragraphs[0].text      # wrapped lines joined without spaces
    assert all(c.char_end - c.char_start <= CJK_MAX_CHARS for p in book.paragraphs for c in p.tts_chunks)


# ---- novel or textbook ----

def test_kind_heuristic_in_spanish():
    novel = "\n".join(["—¿Vienes conmigo? —preguntó Ana con una sonrisa.", "—No puedo, tengo que trabajar.",
                       "Ella se encogió de hombros y salió de la casa sin mirar atrás."] * 30)
    assert detect_mod.heuristic(novel)[0] == "novel"
    textbook = "\n".join(["Figura 2.1 muestra el índice invertido. Definición: una lista de apariciones.",
                          "Ejemplo 3: calcule la frecuencia. Ejercicios al final del capítulo.",
                          "Tabla 2.2 resume los tamaños de la colección."] * 30)
    assert detect_mod.heuristic(textbook)[0] == "textbook"


def test_kind_heuristic_for_english_is_unchanged():
    text = TEXTBOOK_EN * 10
    assert detect_mod.heuristic(text) == detect_mod.heuristic(text, "eng")


# ---- search ----

def test_bm25_tokenizes_cjk_as_bigrams_and_english_as_before():
    assert retrieval.tokenize("Harry's wand, 1888!") == ["harry", "s", "wand", "1888"]
    assert retrieval.tokenize("曹操说") == ["曹操", "操说"]
    assert retrieval.tokenize("第3章") == ["第", "3", "章"]


def test_bm25_finds_a_chinese_passage():
    texts = ["刘备与关羽张飞在桃园结义。", "曹操带兵攻打徐州，百姓四散。", "诸葛亮在隆中草庐中读书。"]
    chunks = [retrieval.Chunk(id=f"r{i}", para_start=i, para_end=i, text=t) for i, t in enumerate(texts)]
    index = retrieval.Index(chunks)
    best = int(index.scores("诸葛亮在哪里读书？").argmax())
    assert best == 2


@pytest.fixture
def translations(monkeypatch):
    multilingual._cache.clear()
    calls = []

    async def fake(text, language=None):
        calls.append((text, language))
        return "¿Quién es la reina?"
    monkeypatch.setattr(multilingual, "translate", fake)
    return calls


def test_a_question_in_the_book_language_is_searched_as_is(translations):
    q = "¿Quién es la reina y por qué llora?"
    assert asyncio.run(multilingual.search_query(q, "spa")) == q
    assert asyncio.run(multilingual.search_query("Ana?", "spa")) == "Ana?"     # unclear: as is
    assert translations == []


def test_a_question_in_another_language_is_searched_in_the_book_language(translations):
    q = "Who is the queen and why does she cry?"
    assert asyncio.run(multilingual.search_query(q, "spa")) == "¿Quién es la reina?"
    assert asyncio.run(multilingual.search_query(q, "spa")) == "¿Quién es la reina?"
    assert translations == [(q, "Spanish")]   # cached, and separately from an English book's cache
    assert asyncio.run(multilingual.search_query(q)) == q                       # English book: as before


# ---- prompts ----

def test_answers_default_to_the_book_language():
    book = book_in("spa", [SPANISH])
    system, _ = personas.build_answer_prompt(book, "narrator", "", [], "Ana?")
    assert system.startswith(personas.BOOK_LANGUAGE_RULE.format(book="Spanish"))
    system, _ = personas.build_answer_prompt(book, "narrator", "", [], "¿Quién es la reina y por qué llora?")
    assert system.startswith(personas.BOOK_LANGUAGE_RULE.format(book="Spanish"))
    system, _ = personas.build_answer_prompt(book, "narrator", "", [], "Who is the queen and why does she cry?")
    assert system.startswith(personas.OTHER_LANGUAGE_RULE.format(lang="English", book="Spanish"))


def test_english_book_prompts_are_unchanged():
    book = book_in("eng", [ENGLISH])
    system, _ = personas.build_answer_prompt(book, "narrator", "", [], "Who is Alice?")
    assert system.startswith("You are the narrator")
    system, _ = personas.build_answer_prompt(book, "narrator", "", [], "¿Quién es la reina y por qué llora?")
    assert system.startswith(personas.LANGUAGE_RULE.format(lang="Spanish"))


def test_public_answer_request_carries_the_language():
    req = AnswerRequest(title="T", query="Ana?", language="spa")
    assert req.prompt()[0].startswith(personas.BOOK_LANGUAGE_RULE.format(book="Spanish"))
    assert AnswerRequest(title="T", query="Who?").prompt()[0].startswith("You are the narrator")
    out = {"story_so_far": "", "passages": []}
    assert answer_payload(book_in("spa", [SPANISH]), None, "narrator", out, "q", False)["language"] == "spa"
    assert "language" not in answer_payload(book_in("eng", [ENGLISH]), None, "narrator", out, "q", False)


def test_enrich_payload_sends_the_language_only_when_not_english():
    assert _payload(book_in("spa", [SPANISH]))["language"] == "spa"
    assert "language" not in _payload(book_in("eng", [ENGLISH]))
    req = EnrichRequest(title="T", language="spa", sections=[{"id": "s1", "title": "", "start_para": 0}],
                        paragraphs=[{"idx": 0, "section_id": "s1", "text": SPANISH}])
    assert req.to_book().language == "spa"


def test_prompt_notes():
    assert booklang.prompt_note("eng") == "" and booklang.segment_note(None) == ""
    assert "written in Spanish" in booklang.prompt_note("spa")
    assert "Write your answer in French" in booklang.prompt_note("fra", write_in_language=True)
    assert "—dijo él—" in booklang.segment_note("spa")


class Recorder:
    """A fake LLM `call`: records the system prompts, answers with a fixed model."""

    def __init__(self, answer):
        self.systems, self.answer = [], answer

    async def __call__(self, tier, system, user, schema, **kw):
        self.systems.append(system)
        return self.answer(schema)


def test_segment_prompt_and_dialogue_marks_for_a_spanish_book():
    ana = Character(id="ana", name="Ana", first_para=0, voice="nora", persona="x")
    text = "—¡Corre! —gritó Ana."
    book = book_in("spa", [text, "Era de noche."], [ana])
    assert seg_mod.needs_llm(book, book.paragraphs[0]) and not seg_mod.needs_llm(book, book.paragraphs[1])

    def answer(schema):   # the model drops the opening dash: it is put back from the paragraph
        items = [seg_mod.SegItem(speaker="ana", text="¡Corre!", emotion=None),
                 seg_mod.SegItem(speaker="narrator", text="—gritó Ana.", emotion=None)]
        if schema is seg_mod.BatchResult:
            return seg_mod.BatchResult(paragraphs=[seg_mod.BatchPara(para=0, segments=items)])
        return seg_mod.SegResult(segments=items)
    rec = Recorder(answer)
    counts = asyncio.run(seg_mod.segment_book(book, rec))
    assert counts.get("llm") == 1
    assert [s.text for s in book.paragraphs[0].segments] == ["—¡Corre!", "—gritó Ana."]
    assert all(s.endswith(booklang.segment_note("spa")) for s in rec.systems)


def test_english_segment_prompt_and_marks_are_unchanged():
    ana = Character(id="ana", name="Ana", first_para=0, voice="nora", persona="x")
    book = book_in("eng", ['"Run!" cried Ana.'], [ana])

    def answer(schema):
        items = [seg_mod.SegItem(speaker="ana", text='"Run!"', emotion=None),
                 seg_mod.SegItem(speaker="narrator", text="cried Ana.", emotion=None)]
        return seg_mod.BatchResult(paragraphs=[seg_mod.BatchPara(para=0, segments=items)])
    rec = Recorder(answer)
    asyncio.run(seg_mod.segment_book(book, rec))
    assert rec.systems == [seg_mod.BATCH_SYSTEM]
    assert seg_mod.quote_marks(book) == seg_mod._QUOTES


def test_cast_and_summary_prompts_are_told_the_language():
    ana = Character(id="ana", name="Ana", first_para=0, voice="nora", persona="x")
    book = book_in("spa", [SPANISH] * 3)

    def answer(schema):
        if schema is cast_mod.MainCast:
            return cast_mod.MainCast(characters=[cast_mod.MainCharacter(
                id="ana", name="Ana", aliases=["Ana"], first_para=0, gender="female", voice="nora", voice_tags="")],
                narrator_voice="nora", narrator_is_character=None)
        if schema is cast_mod.PersonaResult:
            return cast_mod.PersonaResult(persona="Habla rápido.")
        return sum_mod.NoteResult(summary="Un hidalgo vive en la Mancha.")
    rec = Recorder(answer)
    asyncio.run(cast_mod.cast_novel(book, rec))
    asyncio.run(sum_mod.build_summaries(book, rec))
    assert len(rec.systems) >= 3 and all("written in Spanish" in s for s in rec.systems)
    assert "Write your answer in Spanish" in rec.systems[-1]
    english = book_in("eng", [ENGLISH] * 3, [ana])
    rec2 = Recorder(answer)
    asyncio.run(sum_mod.build_summaries(english, rec2))
    assert rec2.systems and all(s == sum_mod.NOTE_SYSTEM for s in rec2.systems)


def test_textbook_notes_are_written_in_the_book_language():
    book = book_in("fra", [FRENCH] * 3)
    book.kind = "textbook"
    rec = Recorder(lambda schema: tb_mod.SectionSummary(summary="Résumé."))
    asyncio.run(tb_mod.build(book, rec))
    assert rec.systems and all("Write your answer in French" in s for s in rec.systems)


def test_english_attribution_rules_are_skipped_for_another_language():
    ana = Character(id="ana", name="Ana", first_para=0, voice="nora", persona="x")
    book = book_in("spa", ['"Hola", said Ana.'], [ana])
    book.paragraphs[0].segments = [Segment(speaker="narrator", text='"Hola", said Ana.')]
    assert pipeline.attribute_speakers(book) == {}
    assert book.paragraphs[0].segments[0].speaker == "narrator"


def test_first_appearance_of_a_chinese_name():
    book = book_in("zho", ["话说天下大势。", "刘备字玄德，涿郡人也。", "关羽来了。"])
    assert cast_mod.locate_first_para(book, ["刘备", "玄德"], 2) == 1


# ---- TTS: the book's language goes to Boson; no word timestamps (chunk-level highlight) ----

@pytest.fixture
def speech_requests(env, monkeypatch, tmp_path):
    client, _ = env
    bodies = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        bodies.append(body)
        if body.get("timestamps"):
            return httpx.Response(200, json={"audio": "TVAz", "timestamps": [{"word": "It", "start": 0, "end": 0.2}]})
        return httpx.Response(200, content=b"MP3BYTES")
    monkeypatch.setattr(main, "boson", BosonClient(transport=httpx.MockTransport(handler)))
    book = book_in("spa", [SPANISH])
    rechunk_book(book)
    d = tmp_path / "books" / "es"
    d.mkdir(parents=True)
    (d / "book.json").write_text(book.model_copy(update={"id": "es"}).model_dump_json(), encoding="utf-8")
    return client, bodies, book


def test_tts_of_a_spanish_book_sends_its_language(speech_requests):
    client, bodies, book = speech_requests
    chunk = book.paragraphs[0].tts_chunks[0]
    r = client.post("/api/tts", json={"book_id": "es", "chunk_id": chunk.id})
    assert r.status_code == 200, r.text
    assert r.json()["timestamps"] is None        # the reader lights the whole chunk
    assert bodies[-1]["language"] == "spa" and bodies[-1]["normalize"] is True and "timestamps" not in bodies[-1]
    assert tts_cache.is_cached(chunk.tts_text, chunk.voice, "spa")
    assert client.post("/api/tts", json={"book_id": "es", "chunk_id": chunk.id}).json()["cached"] is True


def test_tts_of_an_english_book_sends_no_language(speech_requests):
    client, bodies, _ = speech_requests
    r = client.post("/api/tts", json={"book_id": "demo", "chunk_id": "p0c0"})
    assert r.status_code == 200, r.text
    assert "language" not in bodies[-1] and "normalize" not in bodies[-1] and bodies[-1]["timestamps"] is True


def test_a_question_in_the_books_own_script_is_answered_in_the_books_language():
    """The question detector reads any Cyrillic as Russian and any Arabic script as Arabic (code review): a Ukrainian or
    Persian book asked in its own script still answers in its own language; another script or English still wins."""
    from app.personas import answer_language
    ukr = book_in("ukr", ["Текст."])
    assert answer_language(ukr, "Хто такий головний герой?") == "Ukrainian"
    assert answer_language(ukr, "Who is the main character in this story?") == "English"
    fas = book_in("fas", ["متن."])
    assert answer_language(fas, "قهرمان داستان کیست؟") == "Persian"
    rus = book_in("rus", ["Текст."])
    assert answer_language(rus, "Кто главный герой?") == "Russian"
