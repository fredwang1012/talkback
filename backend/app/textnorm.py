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


def normalize_for_tts(text: str) -> str:
    """Light normalization: spell out years and plain integers; leave punctuation and 'Mr.' as is."""
    def repl(m: re.Match) -> str:
        s = m.group(0)
        n = int(s.replace(",", ""))
        if len(s) == 4 and "," not in s and 1100 <= n <= 2099:
            return _year_words(n)
        return _int_words(n)
    return re.sub(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\b", repl, text)
