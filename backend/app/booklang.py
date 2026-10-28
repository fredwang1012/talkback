"""The language a book is written in (#117), as the ISO 639-3 code Higgs TTS takes (tts_lang), and what follows from it.

Detection is cheap and offline: the script of the letters first (Chinese, Japanese, Korean, Cyrillic, Arabic, ...), then,
for Latin script, the share of each language's most common words. It leans to English: a text is only called another
Latin-script language when that language's common words clearly outnumber the English ones, so an English upload is
never mistaken for Spanish. Anything unclear (too little text, an unknown language) is English, the app's old behaviour.

English books ("eng", the default of Book.language) keep every rule as it was. Another language turns off the
English-only rules (number spelling, speech tags, sound effects, singing) and tells the LLM prompts which language the
book is in.
"""
import re

from . import tts_lang

ENGLISH = "eng"
SAMPLE_CHARS = 30_000
MIN_LETTERS = 20          # fewer letters than this: too little to go on (English)
MIN_SHARE = 0.10          # a Latin-script language needs this share of its common words among all words ...
OVER_ENGLISH = 1.5        # ... and this many times the English share

_CJK_HAN = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_KANA = re.compile(r"[぀-ヿ]")
_HANGUL = re.compile(r"[가-힯ᄀ-ᇿ]")
_SCRIPTS = [  # (code, letters of the script, letters only that language of the script uses, its code)
    ("rus", re.compile(r"[Ѐ-ӿ]"), re.compile(r"[іїєґІЇЄҐ]"), "ukr"),
    ("ara", re.compile(r"[؀-ۿ]"), re.compile(r"[پچژگ]"), "fas"),
    ("hin", re.compile(r"[ऀ-ॿ]"), None, None),
    ("heb", re.compile(r"[֐-׿]"), None, None),
    ("ell", re.compile(r"[Ͱ-Ͽ]"), None, None),
    ("tha", re.compile(r"[฀-๿]"), None, None),
]
_STOPWORDS = {
    "eng": "the and of to was he she his her that it with is you for had not but they at have this from which said were "
           "be him what when there would been could my me",
    "spa": "el los las del y que por con para una se su al lo como más pero sus fue muy sin sobre también hasta hay "
           "donde él ella yo dijo está era había cuando porque esto eso ya les",
    "fra": "le les des du et est une il elle je ne pas qui dans sur au aux ce cette avec pour par mais était avait son "
           "sa ses nous vous lui dit plus comme tout ou être",
    "deu": "der die das und ist nicht ein eine ich sie er es zu den dem mit auf sich des von war hatte wie auch aber "
           "wenn nach noch aus wurde bei dass sagte einen einem im",
    "ita": "il lo gli di che è non per della del con come si sono questo lei lui ma era aveva alla nel nella anche più "
           "mi ha io da dei delle disse quando perché",
    "por": "o os não uma com do da dos das em no na é ao mas foi ele ela seu sua também isso você pelo pela mais "
           "muito já eu estava disse tinha",
    "nld": "de het een en van ik te dat die niet op aan met hij zij ze maar voor er als om zijn naar ook wat nog bij "
           "uit dan zei",
    "pol": "i w z na się nie że do to jest jak ale po co tak za od był była tylko jego jej mnie przez już ja go bardzo "
           "który która",
}
_STOPWORDS = {code: frozenset(words.split()) for code, words in _STOPWORDS.items()}
_WORDS = re.compile(r"[^\W\d_]+")


def sample(text: str, total: int = SAMPLE_CHARS) -> str:
    """About `total` chars from the start, middle and end (front matter alone can be in another language)."""
    if len(text) <= total:
        return text
    part = total // 3
    mid = len(text) // 2 - part // 2
    return " ".join((text[:part], text[mid:mid + part], text[-part:]))


def detect(text: str) -> str:
    """ISO 639-3 code of the language `text` is written in; "eng" when unclear."""
    s = sample(text or "")
    letters = sum(c.isalpha() for c in s)
    if letters < MIN_LETTERS:
        return ENGLISH
    han, kana, hangul = len(_CJK_HAN.findall(s)), len(_KANA.findall(s)), len(_HANGUL.findall(s))
    if han + kana + hangul > 0.3 * letters:
        if kana > 0.1 * (han + kana):
            return "jpn"
        return "kor" if hangul > han else "zho"
    for code, script, own, own_code in _SCRIPTS:
        n = len(script.findall(s))
        if n > 0.5 * letters:
            return own_code if own is not None and len(own.findall(s)) > 0.005 * n else code
    words = [w.lower() for w in _WORDS.findall(s)]
    if len(words) < 10:
        return ENGLISH
    shares = {code: sum(w in common for w in words) / len(words) for code, common in _STOPWORDS.items()}
    english = shares.pop(ENGLISH)
    best = max(shares, key=shares.get)
    if shares[best] >= MIN_SHARE and shares[best] > english * OVER_ENGLISH:
        return best
    return ENGLISH


def is_english(code: str | None) -> bool:
    return tts_lang.is_english(code)


def is_cjk(code: str | None) -> bool:
    """Written without spaces between words (Chinese, Japanese)."""
    return code in ("zho", "cmn", "jpn")


_CJK_WORDY = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")


def word_count(text: str) -> int:
    """Words of `text` for length limits: space-separated words, plus one per two Chinese / Japanese characters
    (written without spaces). Text without such characters counts exactly as len(text.split())."""
    return len(text.split()) + len(_CJK_WORDY.findall(text)) // 2


def name(code: str | None) -> str:
    """English name of the language ("Spanish"), for prompts."""
    if is_english(code):
        return "English"
    return tts_lang.LANGUAGES.get("zho" if code == "cmn" else code, code or "English")


def tts_language(code: str | None) -> str | None:
    """The `language` to send Boson for a book's chunks: None for English (the request stays as it always was)."""
    return None if is_english(code) or not tts_lang.valid(code) else code


# ---- dialogue marks ----

# French « », German „ “ (and » «), Chinese / Japanese 「」『』; Spanish and French also open a line of dialogue with a
# dash ("—¿Vienes? —preguntó él."). Curly double quotes are made straight by parsing.normalize.
OPEN_QUOTES = "\"“«»„「『"
_DASH_LINE = re.compile(r"^\s*[—―–]\s*\S")


def has_dialogue(text: str, code: str | None) -> bool:
    """True when a paragraph may hold dialogue. English keeps the old test (a straight double quote)."""
    if is_english(code):
        return '"' in text
    return any(q in text for q in OPEN_QUOTES) or bool(_DASH_LINE.match(text))


# ---- prompts ----

def prompt_note(code: str | None, *, write_in_language: bool = False) -> str:
    """A rule appended to an LLM system prompt for a book that is not in English ("" for English, so the English
    prompts stay byte for byte the same). `write_in_language`: what the model writes (notes, summaries) is read to the
    reader, so it must be in the book's language too."""
    if is_english(code):
        return ""
    lang = name(code)
    out = (f"\n\nThe book is written in {lang}. Copy its text, names and quotes exactly as the book writes them; never "
           "translate them.")
    if write_in_language:
        out += f" Write your answer in {lang}."
    return out


SEGMENT_NOTE = ("\n- Dialogue in this book may be marked with « », „ “, 「 」 or 『 』 instead of quotation marks, or opened "
                "by a dash (—) as in Spanish and French: the dash and the spoken words belong to the character; a "
                "narrator's clause between dashes inside the line (—dijo él—, —dit-il) belongs to the narrator.")


def segment_note(code: str | None) -> str:
    """The dialogue-marks rule for the segmenter, plus the book's language ("" for English)."""
    return "" if is_english(code) else SEGMENT_NOTE + prompt_note(code)
