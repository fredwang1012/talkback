"""Split paragraphs into Boson TTS chunks (<= 280 chars, sentence-aligned). Shared by preprocess/chunk_tts.py and uploads."""
import logging
import re

from . import booklang
from .delivery import SONG_MAX_PARAS, apply_styles, detect_expressive, is_scene_break, speech_tag, sung_from, with_sfx
from .schema import Book, Paragraph, Segment, TtsChunk
from .textnorm import may_change, normalize_for_tts

MAX_CHARS = 280
CJK_MAX_CHARS = 120   # Chinese / Japanese say far more per character: a chunk of the same length would run ~4x as long
# Bump whenever chunk_paragraph's output can change (tags, pauses, sfx, splitting, normalisation): every book built
# with a lower Book.chunk_rules is re-chunked once by the server (books.load_book). History:
#   1: expressive delivery (#73: lead pauses, sfx, expressive_*) and singing (#74). Books without the field are 0.
#   2: sneeze and humming sound effects (#135).
#   3: MAX_CHARS bounds the spoken text after number spelling / normalize_for_tts, not the raw text; "e.g." / "i.e."
#      never end a sentence.
CHUNK_RULES_VERSION = 3
DEFAULT_VOICE = "default"
log = logging.getLogger("talkback.ttschunks")
# Honorifics and common abbreviations: a period after one never ends a sentence ("Mr. Darling", "e.g. Wendy").
ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "messrs", "capt", "col", "prof", "no", "vs", "etc", "sr", "jr", "gen", "lt",
                 "e.g", "i.e"}

_SENT_END = re.compile(r"""(?<![.!?])[.!?]++["')\]]*+(?=\s)""")  # lookbehind + possessive: linear on punctuation runs


_WS = re.compile(r"\s*")
_NONWS = re.compile(r"\S")
_LAST_WORD = re.compile(r"[A-Za-z]+\.?$")
_INITIAL = re.compile(r"(?:^|\s)[A-Z]\.$")
_TAIL = 40  # abbreviation / initial checks only need the last few characters before the period
_OPENERS = "\"'([{“‘"
# Books in other languages (#117, Book.language): their own abbreviations, closing quotes after the sentence end
# (French sets a space before "!" and "»"), a dash before a lowercase word continuing the sentence (Spanish
# "—¿Vienes? —preguntó él."), and full-width sentence ends in Chinese / Japanese (no space follows them).
FOREIGN_ABBREVIATIONS = {
    "spa": {"sr", "sra", "srta", "dr", "dra", "ud", "uds", "etc", "pág", "núm", "av", "lic", "ing", "prof"},
    "fra": {"m", "mme", "mlle", "mm", "dr", "etc", "p", "cf", "st", "ste", "prof"},
    "deu": {"hr", "fr", "dr", "nr", "bzw", "usw", "z.b", "d.h", "ca", "prof", "st"},
    "ita": {"sig", "sig.ra", "dott", "prof", "ecc", "pag"},
    "por": {"sr", "sra", "dr", "dra", "etc", "pág", "prof"},
    "nld": {"dhr", "mevr", "dr", "bijv", "enz", "prof", "st"},
}
_SENT_END_FOREIGN = re.compile(r"""(?<![.!?…])[.!?…]++(?:\s?[»"'”’)\]])*+(?=\s)""")
_SENT_END_CJK = re.compile(r"[。！？!?…]+[」』”’\"'）)]*")
_DASH_LETTER = re.compile(r"[—―–]\s*([^\W\d_])")
_CLAUSE_CJK = ("，", "；", "：", "、")   # Chinese / Japanese clause marks (no space after them)


def ends_with_abbreviation(before: str) -> bool:
    """True when `before` (text up to and including a period) ends with an honorific or common abbreviation
    ("Mr.", "(Dr.", "e.g."), so that period does not end the sentence."""
    if not before.endswith("."):
        return False
    last_word = _LAST_WORD.search(before)
    if last_word and last_word.group().rstrip(".").lower() in ABBREVIATIONS:
        return True
    token = before.split()[-1].lstrip(_OPENERS).rstrip(".").lower() if before.strip() else ""
    return token in ABBREVIATIONS


def sentence_spans(text: str, language: str | None = None) -> list[tuple[int, int]]:
    """Return (start, end) offsets of sentences; spans cover all non-whitespace text. Linear in len(text).
    `language` (ISO 639-3, Book.language): None or "eng" keeps the English rules."""
    if not booklang.is_english(language):
        return _foreign_sentence_spans(text, language)
    spans: list[tuple[int, int]] = []
    start = 0
    for m in _SENT_END.finditer(text):
        if m.start() < start:
            continue
        end = m.end()
        before = text[max(start, m.start() + 1 - _TAIL):m.start() + 1]
        nxt = _WS.match(text, end).end()
        if m.group().rstrip("\"')]") == ".":  # a lone period, not "?!" or an ellipsis
            if ends_with_abbreviation(before) or _INITIAL.search(before):
                continue  # "Mr.", initials such as "J. Smith"
        if text[nxt:nxt + 1].islower():
            continue  # ellipsis, dialogue tag ('"Well?" said he.'), or other lowercase continuation
        spans.append((start, end))
        start = nxt
    first = _NONWS.search(text, start)
    if first:
        spans.append((start, len(text.rstrip())))
    return spans


def _foreign_sentence_spans(text: str, language: str | None) -> list[tuple[int, int]]:
    """sentence_spans for a book that is not in English: CJK sentence ends need no space after them; elsewhere a
    lowercase word, or a dash before one, continues the sentence, and the language's own abbreviations apply."""
    cjk = booklang.is_cjk(language)
    abbreviations = FOREIGN_ABBREVIATIONS.get(language or "", set())
    spans: list[tuple[int, int]] = []
    start = 0
    for m in (_SENT_END_CJK if cjk else _SENT_END_FOREIGN).finditer(text):
        if m.start() < start:
            continue
        end = m.end()
        nxt = _WS.match(text, end).end()
        if not cjk:
            if m.group().rstrip("\"'”’)]» ") == ".":  # a lone period, not "?!" or an ellipsis
                before = text[max(start, m.start() + 1 - _TAIL):m.start() + 1]
                word = before.split()[-1].lstrip(_OPENERS + "«¿¡—").rstrip(".").lower() if before.strip() else ""
                if word in abbreviations or _INITIAL.search(before):
                    continue
            dash = _DASH_LETTER.match(text, nxt)
            if text[nxt:nxt + 1].islower() or (dash and dash.group(1).islower()):
                continue
        spans.append((start, end))
        start = nxt
    first = _NONWS.search(text, start)
    if first:
        spans.append((start, len(text.rstrip())))
    return spans


def max_chars(language: str | None = None) -> int:
    """Longest spoken text of one TTS chunk for a book in `language`."""
    return CJK_MAX_CHARS if booklang.is_cjk(language) else MAX_CHARS


def split_long(text: str, base: int, limit: int = MAX_CHARS) -> list[tuple[int, int]]:
    """Split an over-long sentence at the last comma/semicolon/colon/dash (else space) before `limit`. Chinese and
    Japanese clause marks count too (they have no space after them)."""
    out: list[tuple[int, int]] = []
    pos = 0
    n = len(text)
    while n - pos > limit:
        window = text[pos:pos + limit]
        cut = max(window.rfind(", "), window.rfind("; "), window.rfind(": "), window.rfind("—"),
                  *(window.rfind(c) for c in _CLAUSE_CJK))
        cut = cut + 1 if cut > limit // 3 else window.rfind(" ")
        if cut <= 0:
            cut = limit
        out.append((base + pos, base + pos + cut))
        pos += cut
        while pos < n and text[pos] == " ":
            pos += 1
    if pos < n:
        out.append((base + pos, base + n))
    return out


def build_tts_text(spoken: str, voice_tags: str, emotion: str | None, style: str | None = None,
                   sfx: bool = False, *, tag: str = "", expressive: str | None = None, pause: str | None = None,
                   language: str | None = None) -> str:
    """[voice prosody tags][style][emotion][expressiveness][pause][spoken text, with an sfx before onomatopoeia if sfx].

    Delivery tags (style, emotion, expressive_*) go at the start; `pause` ('pause' / 'long_pause') is positional and sits
    right before the text. `tag` is the adjacent speech tag, which some sound effects need (see delivery.sfx_at).
    An expressive_* already in the voice tags is not repeated. A book in another `language` is sent as written: its
    chunks are requested without word timestamps, so Boson normalizes numbers and symbols in that language."""
    if expressive and "expressive_" in voice_tags:
        expressive = None
    tags = (voice_tags + (f"<|style:{style}|>" if style else "") + (f"<|emotion:{emotion}|>" if emotion else "")
            + (f"<|prosody:{expressive}|>" if expressive else "") + (f"<|prosody:{pause}|>" if pause else ""))
    text = normalize_for_tts(spoken) if booklang.is_english(language) else spoken
    return tags + (with_sfx(text, tag) if sfx else text)


class BookIndex:
    """What lead_pause / sing_offset look up for every paragraph, built once per book: the section start paragraphs
    and paragraph idx -> paragraph (rechunk_book on 30k paragraphs x 3k sections went from ~10 s to linear)."""

    def __init__(self, book: Book):
        self.book, self.paragraphs, self.sections = book, book.paragraphs, book.sections
        self.n_paragraphs, self.n_sections = len(book.paragraphs), len(book.sections)
        self.starts = frozenset(s.start_para for s in book.sections)
        self.by_idx: dict[int, Paragraph] = {}
        for q in book.paragraphs:
            self.by_idx.setdefault(q.idx, q)   # the first paragraph with an idx, as the old linear scan found

    def fits(self, book: Book) -> bool:
        return (self.book is book and self.paragraphs is book.paragraphs and self.sections is book.sections
                and self.n_paragraphs == len(book.paragraphs) and self.n_sections == len(book.sections))


_last_index: BookIndex | None = None   # single-paragraph calls (no index passed) on the same book reuse one index


def book_index(book: Book) -> BookIndex:
    global _last_index
    idx = _last_index
    if idx is None or not idx.fits(book):
        idx = _last_index = BookIndex(book)
    return idx


def _prev_paragraph(p: Paragraph, book: Book, index: BookIndex | None = None) -> Paragraph | None:
    ps = book.paragraphs
    if 0 < p.idx <= len(ps) and ps[p.idx - 1].idx == p.idx - 1:
        return ps[p.idx - 1]
    return (index or book_index(book)).by_idx.get(p.idx - 1)


def lead_pause(p: Paragraph, book: Book, index: BookIndex | None = None) -> str | None:
    """Positional pause before a paragraph's first chunk (#73), or None.

    long_pause: first paragraph of a section (after the chapter heading) or right after a scene break ('* * *').
    pause: a dialogue paragraph right after a paragraph of pure narration that ended a sentence."""
    if is_scene_break(p.text):
        return None
    index = index or book_index(book)
    if p.idx in index.starts:
        return "long_pause"
    prev = _prev_paragraph(p, book, index)
    if prev is None or prev.kind == "code":
        return None
    if is_scene_break(prev.text):
        return "long_pause"
    chars = {c.id for c in book.characters}
    first = next((s for s in p.segments if s.text.strip()), None)
    if first is None or first.speaker not in chars:
        return None
    prev_segs = [s for s in prev.segments if s.text.strip()]
    if prev_segs and all(s.speaker not in chars for s in prev_segs) \
            and prev.text.rstrip().rstrip("\"'”’)»」』").endswith((".", "!", "?", "。", "！", "？")):
        return "pause"
    return None


def _groups(p: Paragraph, text: str, base: int, language: str | None = None) -> list[tuple[int, int]]:
    """Whole sentences packed greedily up to MAX_CHARS (over-long sentences split first), as paragraph offsets.
    Measured by what is actually read (spoken_len): inline math (#41) read out, numbers and simple math spelled out.
    `language`: the book's (its sentence rules and, for Chinese / Japanese, the smaller CJK_MAX_CHARS)."""
    limit = max_chars(language)
    pieces: list[tuple[int, int]] = []
    for s, e in sentence_spans(text, language):
        if e - s > limit:
            pieces += split_long(text[s:e], base + s, limit)
        else:
            pieces.append((base + s, base + e))
    pieces = [q for s, e in pieces for q in _fit_pieces(p, s, e, language)]
    groups: list[tuple[int, int]] = []
    for s, e in pieces:
        if groups and spoken_len(p, groups[-1][0], e, language) <= limit:
            groups[-1] = (groups[-1][0], e)
        else:
            groups.append((s, e))
    return groups


def sing_offset(p: Paragraph, book: Book, index: BookIndex | None = None) -> int | None:
    """Offset in p.text from which the paragraph is sung (#74), or None. Looks back over the same chapter only."""
    if p.kind == "code" or len(p.text) > 4000:
        return None
    index = index or book_index(book)
    prev: list[str] = []
    cur = p
    while len(prev) <= SONG_MAX_PARAS and cur.idx not in index.starts:
        cur = _prev_paragraph(cur, book, index)
        if cur is None:
            break
        prev.insert(0, cur.text)
    return sung_from(prev, p.text)


CODE_SPOKEN = "Code listing skipped."
EQUATION_SPOKEN = "Equation."
# the same notes in the languages textbooks are most often uploaded in (#117); any other language hears the English
FOREIGN_CODE_SPOKEN = {"spa": "Se omite un listado de código.", "fra": "Listing de code omis.",
                       "deu": "Codeauflistung übersprungen.", "ita": "Listato di codice omesso.",
                       "por": "Listagem de código omitida.", "zho": "此处略过一段代码。", "jpn": "ここでコードは省略します。"}
FOREIGN_EQUATION = {"spa": "Ecuación", "fra": "Équation", "deu": "Gleichung", "ita": "Equazione", "por": "Equação",
                    "zho": "公式", "jpn": "数式"}


def _fit(text: str, limit: int = MAX_CHARS) -> str:
    """`text` cut at the last clause / word boundary so that it fits in one TTS chunk."""
    if len(text) <= limit:
        return text
    head = text[:limit - 1]
    cut = max(head.rfind(", "), head.rfind("; "))
    cut = cut if cut > limit // 2 else head.rfind(" ")
    return (head[:cut] if cut > 0 else head).rstrip(" ,;") + "."


def equation_tts_text(p: Paragraph, language: str | None = None) -> str:
    """What is said for a display equation: the model's reading, else the extracted text when it reads faithfully,
    else just "Equation 3.4." One chunk (<= MAX_CHARS after normalization). In another `language` the label is
    translated where known, the model's reading (plain English, enrich/equations.py) is not used, and the text is left
    to Boson's normalization."""
    from .parsing import split_equation_number
    m = p.math
    number = m.number if m else None
    if not booklang.is_english(language):
        label = FOREIGN_EQUATION.get(language or "", "Equation") + (f" {number}" if number else "")
        if m is not None and not m.garbled:
            spoken = split_equation_number(p.text)[0] or f"{label}."
        else:
            spoken = f"{label}."
        return _fit(spoken, max_chars(language))
    head, *rest = number.split(".") if number else [""]
    label = " point ".join(["Equation " + head] + [" ".join(r) for r in rest]) if number else "Equation"
    if m is not None and m.spoken:
        spoken = f"{label}: {m.spoken.strip().rstrip('.')}."
    elif m is not None and not m.garbled:
        spoken = split_equation_number(p.text)[0] or EQUATION_SPOKEN
    else:
        spoken = f"{label}."
    return _fit(build_tts_text(spoken, "", None))


INLINE_SPOKEN_MAX = 120  # a longer reading of an inline formula is not used (the shown text is read instead)


def inline_spans(p: Paragraph, s: int, e: int) -> list[tuple[int, int, str]]:
    """(start, end, spoken) relative to `s` of the inline formulas inside p.text[s:e] that have a usable reading."""
    return [(m.start - s, m.end - s, m.spoken.strip()) for m in p.inline_math
            if s <= m.start and m.end <= e and m.spoken and m.spoken.strip()
            and len(m.spoken.strip()) <= INLINE_SPOKEN_MAX]


def spoken_text(p: Paragraph, s: int, e: int) -> str:
    """p.text[s:e] with each inline formula replaced by its reading."""
    text = p.text[s:e]
    for a, b, spoken in reversed(inline_spans(p, s, e)):
        text = text[:a] + spoken + text[b:]
    return text


def spoken_len(p: Paragraph, s: int, e: int, language: str | None = None) -> int:
    """Length of the text Boson reads for p.text[s:e] (before delivery tags): inline formulas replaced by their
    reading, then normalize_for_tts (numbers, years, simple math spelled out). This is what MAX_CHARS bounds.
    A book in another `language` is read as written (see build_tts_text; the inline readings are English)."""
    if not booklang.is_english(language):
        return e - s
    text = spoken_text(p, s, e)
    return len(normalize_for_tts(text)) if may_change(text) else len(text)


_MIN_SPLIT = 12   # never cut a piece into raw windows shorter than this (one long spelled-out number still fits)


def _fit_pieces(p: Paragraph, s: int, e: int, language: str | None = None) -> list[tuple[int, int]]:
    """Split [s, e) (at clause, else word boundaries) until each piece's spoken length fits in MAX_CHARS. The raw
    window shrinks in proportion to how much the text grows when spoken ("777,777" -> 41 chars)."""
    n = spoken_len(p, s, e, language)
    most = max_chars(language)
    if n <= most or e - s <= _MIN_SPLIT:
        return [(s, e)]
    limit = max(_MIN_SPLIT, min(e - s - 1, (e - s) * most // n))
    pieces = split_long(p.text[s:e], s, limit)
    if len(pieces) <= 1:
        return [(s, e)]
    return [q for a, b in pieces for q in _fit_pieces(p, a, b, language)]


def chunk_paragraph(p: Paragraph, book: Book, *, expressive: bool = True, index: BookIndex | None = None) -> list[TtsChunk]:
    """TTS chunks for one paragraph. expressive=False leaves out the #73 additions (pauses, expressive_*, sfx other
    than laughter context), which scripts/estimate_expressive.py uses to measure what they change. `index` (from
    book_index) saves the per-book lookups when chunking many paragraphs.

    A book in another language (#117) keeps the pauses and the styles / emotions the enrich LLM set, but none of the
    English-only rules: rule-based whisper / shout, speech-tag sound effects and expressiveness, singing, and number
    spelling (Boson normalizes the text in its own language)."""
    narrator_voice = book.narrator.voice if book.narrator else DEFAULT_VOICE
    lang = book.language
    english = booklang.is_english(lang)
    if p.kind == "code":  # a listing is displayed whole but spoken as one short note
        note = CODE_SPOKEN if english else FOREIGN_CODE_SPOKEN.get(lang, CODE_SPOKEN)
        return [TtsChunk(id=f"p{p.idx}c0", speaker="narrator", voice=narrator_voice,
                         tts_text=build_tts_text(note, "", None), char_start=0, char_end=len(p.text))]
    if p.kind == "math":
        return [TtsChunk(id=f"p{p.idx}c0", speaker="narrator", voice=narrator_voice,
                         tts_text=equation_tts_text(p, lang), char_start=0, char_end=len(p.text))]
    chars = {c.id: c for c in book.characters}
    segments = p.segments or [Segment(speaker="narrator", text=p.text)]
    if english:
        apply_styles(segments)  # rule-based whispering/shouting; keeps a style the enrich LLM already set
    chunks: list[TtsChunk] = []
    cursor = 0
    pause = lead_pause(p, book, index) if expressive else None
    sing_at = sing_offset(p, book, index) if expressive and english else None
    for si, seg in enumerate(segments):
        seg_text = seg.text.strip()
        if not seg_text:
            continue
        base = p.text.find(seg_text, cursor)
        if base < 0:  # segment does not match the paragraph; callers validate, so degrade to whole paragraph
            raise ValueError(f"paragraph {p.idx}: segment text not found: {seg_text[:40]!r}")
        cursor = base + len(seg_text)
        if seg.speaker in chars:
            voice, tags = chars[seg.speaker].voice, chars[seg.speaker].voice_tags
        else:
            voice, tags = narrator_voice, ""
        is_char = seg.speaker in chars
        extra = ({"tag": speech_tag(segments, si), "expressive": detect_expressive(segments, si)}
                 if expressive and is_char and english else {})
        parts = [(base, seg_text)]
        if sing_at is not None and base < sing_at < cursor:  # the song starts inside this segment (after 'sing:')
            parts = [(base, p.text[base:sing_at].rstrip()), (sing_at, p.text[sing_at:cursor])]
        for part_base, part_text in parts:
            sung = sing_at is not None and part_base >= sing_at
            for s, e in _groups(p, part_text, part_base, lang):
                spoken = spoken_text(p, s, e) if english else p.text[s:e]  # inline readings are English
                if not expressive:
                    text = _legacy_tts_text(spoken, tags, seg.emotion, seg.style, is_char)
                elif sung:  # singing replaces whisper/shout; no sfx or expressive_* inside a song
                    text = build_tts_text(spoken, tags, seg.emotion, "singing", pause=pause)
                else:
                    text = build_tts_text(spoken, tags, seg.emotion, seg.style, sfx=is_char and english, pause=pause,
                                          language=lang, **extra)
                pause = None  # only the paragraph's first chunk
                chunks.append(TtsChunk(
                    id=f"p{p.idx}c{len(chunks)}", speaker=seg.speaker if is_char else "narrator",
                    voice=voice, tts_text=text, char_start=s, char_end=e))
    return chunks


def rechunk_book(book: Book, strict: bool = True) -> None:
    """Rebuild every paragraph's tts_chunks with the current rules and stamp CHUNK_RULES_VERSION. Keeps segment
    styles (apply_styles only fills in missing ones). A segment that does not match its paragraph raises ValueError,
    unless strict=False (the upload pipeline): that paragraph is then read by the narrator instead, so one bad
    paragraph never fails the whole book."""
    index = BookIndex(book)   # fresh: segments / sections may have changed since any cached one
    bad: list[int] = []
    for p in book.paragraphs:
        try:
            p.tts_chunks = chunk_paragraph(p, book, index=index)
        except ValueError:
            if strict:
                raise
            bad.append(p.idx)
            p.segments = [Segment(speaker="narrator", text=p.text.strip())]
            p.tts_chunks = chunk_paragraph(p, book, index=index)
    if bad:  # paragraph numbers only, never book text
        log.warning("%s: %d paragraph(s) read by the narrator, segments did not match the text: paragraph %s",
                    book.id, len(bad), ", ".join(map(str, bad[:20])))
    book.chunk_rules = CHUNK_RULES_VERSION


def needs_rechunk(book: Book) -> bool:
    return (book.chunk_rules or 0) < CHUNK_RULES_VERSION


def _legacy_tts_text(spoken: str, voice_tags: str, emotion: str | None, style: str | None, sfx: bool) -> str:
    """tts_text as built before #73 (laughter was the only sfx). Only for estimating re-render cost."""
    from .delivery import with_laughter
    tags = voice_tags + (f"<|style:{style}|>" if style else "") + (f"<|emotion:{emotion}|>" if emotion else "")
    text = normalize_for_tts(spoken)
    return tags + (with_laughter(text) if sfx else text)


# ---- translated text (bilingual reading, B3) ----

# A sentence ends at . ! ? (followed by space) or at CJK full-width punctuation (no space follows it in CJK text).
_T_SENT_END = re.compile(r"""[.!?]+["')\]»”’]*(?=\s)|[。！？!?…]+[」』”’)）]*""")
_T_CLAUSE = re.compile(r"[,;:，、；：—]")


def _cut_long(text: str, limit: int) -> list[str]:
    """Split one over-long sentence at the last clause mark, else space, else hard at `limit`."""
    out = []
    while len(text) > limit:
        window = text[:limit]
        cut = max((m.end() for m in _T_CLAUSE.finditer(window)), default=-1)
        if cut <= limit // 3:
            cut = window.rfind(" ")
        if cut <= limit // 3:
            cut = limit
        out.append(text[:cut])
        text = text[cut:]
    if text:
        out.append(text)
    return out


def split_translation(text: str, language: str | None = None, limit: int = MAX_CHARS) -> list[str]:
    """A translated piece cut into TTS inputs of at most `limit` characters, whole sentences packed greedily. Knows
    full-width CJK sentence ends (。！？), which have no space after them. Each part is stripped."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    sentences, start = [], 0
    for m in _T_SENT_END.finditer(text):
        sentences.append(text[start:m.end()])
        start = m.end()
    if start < len(text):
        sentences.append(text[start:])
    pieces = [q for s in sentences for q in (_cut_long(s, limit) if len(s) > limit else [s])]
    groups: list[str] = []
    for s in pieces:
        if groups and len((groups[-1] + s).strip()) <= limit:
            groups[-1] += s
        else:
            groups.append(s)
    return [g.strip() for g in groups if g.strip()]
