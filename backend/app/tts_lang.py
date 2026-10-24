"""Languages of Boson Higgs TTS 3 (docs.boson.ai/models/higgs-tts/languages: 102) as ISO 639-3 codes.

The docs list language names only, so the codes are our mapping; Boson answers 400 invalid_field_value for a code it
does not know. Word timestamps exist for English, Chinese and Spanish only (docs: word-level timestamps), and
`timestamps: true` turns normalization off."""

LANGUAGES = {
    "afr": "Afrikaans", "ara": "Arabic", "hye": "Armenian", "asm": "Assamese", "ast": "Asturian", "aze": "Azerbaijani",
    "bak": "Bashkir", "eus": "Basque", "bel": "Belarusian", "ben": "Bengali", "bos": "Bosnian", "bul": "Bulgarian",
    "cat": "Catalan", "ceb": "Cebuano", "ckb": "Central Kurdish", "zho": "Chinese", "hrv": "Croatian", "ces": "Czech",
    "dan": "Danish", "nld": "Dutch", "mhr": "Eastern Mari", "eng": "English", "epo": "Esperanto", "est": "Estonian",
    "fin": "Finnish", "fra": "French", "glg": "Galician", "kat": "Georgian", "deu": "German", "ell": "Greek",
    "guj": "Gujarati", "hat": "Haitian Creole", "hau": "Hausa", "heb": "Hebrew", "hin": "Hindi", "hun": "Hungarian",
    "ind": "Indonesian", "ita": "Italian", "jpn": "Japanese", "jav": "Javanese", "kan": "Kannada", "kaz": "Kazakh",
    "kor": "Korean", "kin": "Kinyarwanda", "kir": "Kyrgyz", "lav": "Latvian", "lin": "Lingala", "lit": "Lithuanian",
    "luo": "Luo", "mkd": "Macedonian", "msa": "Malay", "mal": "Malayalam", "mlt": "Maltese", "mri": "Maori",
    "mar": "Marathi", "mon": "Mongolian", "nep": "Nepali", "nor": "Norwegian", "oci": "Occitan", "fas": "Persian",
    "pol": "Polish", "por": "Portuguese", "ron": "Romanian", "rus": "Russian", "nso": "Sepedi", "srp": "Serbian",
    "sna": "Shona", "slk": "Slovak", "slv": "Slovene", "spa": "Spanish", "swa": "Swahili", "swe": "Swedish",
    "tgl": "Tagalog", "tgk": "Tajik", "tam": "Tamil", "tel": "Telugu", "tha": "Thai", "tur": "Turkish",
    "ukr": "Ukrainian", "urd": "Urdu", "uig": "Uyghur", "uzb": "Uzbek", "vie": "Vietnamese", "xho": "Xhosa",
    "zul": "Zulu",
    # the docs' second table (WER/CER between 5 and 10)
    "sqi": "Albanian", "nya": "Chichewa/Nyanja", "pan": "Eastern Punjabi", "lug": "Ganda", "isl": "Icelandic",
    "gle": "Irish", "kab": "Kabyle", "kea": "Kabuverdianu", "kam": "Kamba", "lat": "Latin", "ltz": "Luxembourgish",
    "orm": "Oromo", "pus": "Pashto", "snd": "Sindhi", "som": "Somali", "umb": "Umbundu", "cym": "Welsh",
}
ALIASES = frozenset({"cmn"})  # Mandarin: the docs' own example code for Chinese
TIMESTAMP_LANGUAGES = frozenset({"eng"})  # Chinese and Spanish have them too, but only with normalization off


def valid(code: str | None) -> bool:
    return bool(code) and (code in LANGUAGES or code in ALIASES)


def is_english(code: str | None) -> bool:
    return code in (None, "eng")


def has_timestamps(code: str | None) -> bool:
    """Boson returns word timestamps for this language, so the normal (timestamped) path applies."""
    return is_english(code) or code in TIMESTAMP_LANGUAGES
