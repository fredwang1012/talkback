"""Light text normalization for TTS. Boson disables its own normalization when timestamps are requested,
so numbers are spelled out here, in one place shared by the chunker and the timestamp aligner."""
import re


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    return _TENS[n // 10] + ("-" + _ONES[n % 10] if n % 10 else "")


def _int_words(n: int) -> str:
    if n < 100:
        return _two(n)
    if n < 1000:
        return _ONES[n // 100] + " hundred" + (" " + _two(n % 100) if n % 100 else "")
    if n < 1000000:
        return _int_words(n // 1000) + " thousand" + (" " + _int_words(n % 1000) if n % 1000 else "")
    return str(n)


def _year_words(n: int) -> str:
    if 2000 <= n <= 2009:
        return "two thousand" + (f" {_ONES[n - 2000]}" if n > 2000 else "")
    hi, lo = divmod(n, 100)
    if lo == 0:
        return f"{_two(hi)} hundred"
    return f"{_two(hi)} {'oh ' + _ONES[lo] if lo < 10 else _two(lo)}"


# ---- spoken math (only the TTS text is changed; the displayed paragraph text stays as it is) ----

_WORDS = {
    "=": "equals", "+": "plus", "−": "minus", "×": "times", "÷": "divided by", "±": "plus or minus",
    "≤": "less than or equal to", "≥": "greater than or equal to", "≠": "not equal to",
    "≈": "approximately equal to", "<": "less than", ">": "greater than", "→": "goes to",
    "∞": "infinity", "°": "degrees", "∝": "proportional to", "√": "square root of",
    "∛": "cube root of", "·": "times", "⋅": "times", "/": "over",
}
_GREEK = dict(zip(
    "αβγδεζηθικλμνξοπρστυφχψωΔΘΛΞΠΣΦΨΩΓ",
    "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi "
    "psi omega Delta Theta Lambda Xi Pi Sigma Phi Psi Omega Gamma".split()))
_GREEK = {k: v.lower() for k, v in _GREEK.items()}
_COMPLEX = set("∑∫∏∂∇∮{}\\")  # sums, integrals, products, partials, braces: say "(equation)"
_SUP = "⁰¹²³⁴⁵⁶⁷⁸⁹⁻ⁿ"
_SUB = "₀₁₂₃₄₅₆₇₈₉"
_SUP_RUN = re.compile("[" + _SUP + "]+")
_SUB_RUN = re.compile("[" + _SUB + "]+")
_SUP_MAP = str.maketrans(_SUP, "0123456789-n")
_SUB_MAP = str.maketrans(_SUB, "0123456789")
_NOT_FRACTIONS = {"w/o", "c/o", "n/a", "b/w", "a/c", "h/w", "s/w", "w/e", "i/o", "r/w", "y/n", "m/f", "a/n"}
_FRACTION = re.compile(r"^(\d{1,4}|[A-Za-z])/(\d{1,4}|[A-Za-z])$")
_EQ_TOKEN = re.compile(r"^[A-Za-z0-9]+[=<>][A-Za-z0-9.]+$")
_WEAK = re.compile(r"^(?:[A-Za-z]|\d+(?:\.\d+)?|d[a-z]|[A-Za-z]_\w{1,3}|[A-Za-z]{1,2}\([^()\s]{0,12}\)|[-*/()\[\]])$")
_AFFIX = re.compile(r"^([(\[]*)(.*?)([.,;:!?)\]]*)$", re.S)
_MATH_CHARS = set(_WORDS) | set(_GREEK) | set(_SUP) | set(_SUB) | set("^") | _COMPLEX
MAX_RUN_TOKENS = 14  # a longer run of math tokens is too much to read out


def _strong(core: str) -> bool:
    """True when the token is unmistakably math (an operator, Greek letter, power, fraction, a=b)."""
    if not core:
        return False
    if core in _WORDS and core != "/":
        return True
    if core.lower() in _NOT_FRACTIONS:
        return False
    if _FRACTION.match(core) or _EQ_TOKEN.match(core):
        return True
    marks = [i for i, c in enumerate(core) if c in _SUP or c in _SUB]
    if marks:  # "x²" and "mc²" are powers; "membrane¹" is a footnote mark
        return 1 <= marks[0] <= 3
    if "^" in core:
        return 0 < core.index("^") <= 3
    return any((ord(c) > 127 and c in _MATH_CHARS) or c in _COMPLEX for c in core)


def _speak_core(core: str) -> str:
    core = re.sub(r"√\(([^()]*)\)", r"√ \1", core)
    core = _SUP_RUN.sub(lambda m: {"2": " squared", "3": " cubed", "n": " to the n"}.get(
        m.group().translate(_SUP_MAP), " to the power of " + m.group().translate(_SUP_MAP).replace("-", "minus ")),
        core)
    core = _SUB_RUN.sub(lambda m: " sub " + m.group().translate(_SUB_MAP), core)
    core = re.sub(r"\^\{?(-?\w+)\}?", lambda m: {"2": " squared", "3": " cubed"}.get(
        m.group(1), " to the " + m.group(1).replace("-", "minus ")), core)
    out = []
    for c in core:
        out.append(f" {_GREEK[c]} " if c in _GREEK else f" {_WORDS[c]} " if c in _WORDS else c)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def speak_math(text: str) -> str:
    """Rewrite simple math in running text as words ("x²" -> "x squared"; "a/b" -> "a over b"; "≤" -> "less than or
    equal to"; Greek letters by name). A run of math tokens that is too complex to read out becomes "(equation)".
    Linear in the text length; ordinary prose (and/or, w/o, C++, footnote marks) is left alone."""
    parts = re.split(r"(\s+)", text)
    toks = [(i, _AFFIX.match(p).groups()) for i, p in enumerate(parts) if i % 2 == 0 and p]
    kinds = []
    for i, (_pre, core, _suf) in toks:
        kinds.append("strong" if _strong(core) else "weak" if (_WEAK.match(core) or not core
                                                               or _WEAK.match(parts[i])) else "")
    out = list(parts)
    n = len(toks)
    k = 0
    while k < n:
        if not kinds[k]:
            k += 1
            continue
        j = k
        while j < n and kinds[j]:
            j += 1
        if "strong" in kinds[k:j]:
            s, e = k, j
            while s < e and kinds[s] != "strong":  # a weak edge ("a" before "+") belongs to the run only inside it
                s += 1
            while e > s and kinds[e - 1] != "strong":
                e -= 1
            run = toks[s:e]
            if e - s > MAX_RUN_TOKENS or any(c in _COMPLEX for _i, (_p, core, _x) in run for c in core):
                run = toks[s:j]  # an equation swallows the weak tokens (numbers, dx) trailing it
                first, last = run[0], run[-1]
                for idx, _g in run:
                    out[idx] = ""
                out[first[0]] = first[1][0] + "(equation)" + last[1][2]
                for idx in range(first[0] + 1, last[0]):
                    if out[idx] and out[idx].isspace():
                        out[idx] = ""
            else:
                for idx, (pre, core, suf) in run:
                    out[idx] = pre + _speak_core(core) + suf
        k = j
    return "".join(out)


def normalize_for_tts(text: str) -> str:
    """Light normalization: spoken math, then spell out years and plain integers; leave punctuation and 'Mr.' as is."""
    text = speak_math(text)

    def repl(m: re.Match) -> str:
        s = m.group(0)
        n = int(s.replace(",", ""))
        if len(s) == 4 and "," not in s and 1100 <= n <= 2099:
            return _year_words(n)
        return _int_words(n)
    return re.sub(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\b", repl, text)
