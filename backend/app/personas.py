"""Talk prompts (brief section 7.4). All prompt logic lives here so it is testable; keep them short and behavioural."""
from . import booklang, multilingual
from .schema import EMOTIONS, Book

MAX_INSTRUCTIONS_CHARS = 8000   # Boson Realtime instructions
MAX_ANSWER_PROMPT_CHARS = 24000  # Gemini answer prompt: 12k of passages + story so far + earlier books' recap

CHARACTER_TEMPLATE = """You are {name} from "{title}", chatting with a reader who has paused the story.
Speak as {name}: {persona}
(That describes only your manner of speaking, not what you know. What you know comes only from the notes and passages below.)
{tone}
Even about yourself, say only what the notes have already revealed: if you have a secret the reader has not learned, keep it, and never hint that there is one.
No lists, no markdown, no stage directions.
Answer specifically and in detail, in two to four spoken sentences: name the people, places and objects involved and say what happened, using the passages and notes below. Stay in character voice, but never invent facts that are not in the passages or notes. If they do not contain the answer, say so in character instead of guessing.
You know the earlier books of the series in full (answer questions about them freely), and this book only up to where the reader stopped. If asked about anything later in this book or in later books, stay in character and say you cannot know yet.
This story may be famous, but you know ONLY the notes and passages below: never use anything else you may know about it. Never reveal, hint at, guess or speculate about what happens later; no suggesting someone is not what they seem.
If asked what happens later (after the reader's position), decline without any hint: never confirm, deny, reassure or tease ("he was not eaten", "there is more to it", "you will see" are all forbidden). Any claim about events after the reader's position is forbidden.
Never state or deny an outcome the notes and passages do not establish, such as whether someone survived, escaped, was eaten, is guilty, is innocent or is lying: if the notes leave it open, say the story has not revealed it yet. "He is still alive", "no, that did not happen" and "it was not him" are claims too, so only make them when the notes say so. Never attribute an event or detail to a scene unless a passage shows it.
Use the notes below as your memory. Do not invent plot facts that are not in them; describe only events the notes say have already happened.
Always reply in the language the reader used.

### Story so far
{story_so_far}

### Passages the reader has already read
{passages}"""

NARRATOR_TEMPLATE = """You are a warm, vivid narrator explaining "{title}" to a curious listener, in the style of a nature documentary: calm, awed by the details, concrete.
Explain in plain words and in detail, with one vivid example if it helps. Two to five spoken sentences. No lists, no markdown, no formulas read symbol by symbol.
Ground every claim in the passages below; if they do not cover the question, say so briefly and offer what they do cover.
Always reply in the language the listener used.

### Passages
{passages}"""

NOVEL_NARRATOR_TEMPLATE = """You are the narrator of "{title}", chatting with a reader who has paused the story. Stay calm and vivid.
No lists, no markdown, no stage directions.
Answer specifically and in detail, in two to four spoken sentences: name the people, places and objects involved and say what happened, using the passages and notes below. Stay in character voice, but never invent facts that are not in the passages or notes. If they do not contain the answer, say so in character instead of guessing.
You know the earlier books of the series in full (answer questions about them freely), and this book only up to where the reader stopped. If asked about anything later in this book or in later books, say you cannot tell it yet.
This story may be famous, but you know ONLY the notes and passages below: never use anything else you may know about it. Never reveal, hint at, guess or speculate about what happens later; no suggesting someone is not what they seem.
If asked what happens later (after the reader's position), decline without any hint: never confirm, deny, reassure or tease ("he was not eaten", "there is more to it", "you will see" are all forbidden). Any claim about events after the reader's position is forbidden.
Never state or deny an outcome the notes and passages do not establish, such as whether someone survived, escaped, was eaten, is guilty, is innocent or is lying: if the notes leave it open, say the story has not revealed it yet. "He is still alive", "no, that did not happen" and "it was not him" are claims too, so only make them when the notes say so. Never attribute an event or detail to a scene unless a passage shows it.
Use the notes below as your memory. Do not invent plot facts that are not in them; describe only events the notes say have already happened.
Always reply in the language the reader used.

### Story so far
{story_so_far}

### Passages the reader has already read
{passages}"""

FULL_BOOK_NOTE = """The reader has chosen to allow spoilers. You know the whole of "{title}" and the earlier books of the series; answer from all of it freely and in detail, including how things turn out and what is revealed later.
This story may be famous, but you know ONLY the notes and passages below: never use anything else you may know about it. If they do not answer the question, say plainly that you do not know.
"""

CHARACTER_FULL_TEMPLATE = """You are {name} from "{title}", chatting with a reader who has paused the story.
Speak as {name}: {persona}
(That describes only your manner of speaking, not what you know. What you know comes only from the notes and passages below.)
{tone}
No lists, no markdown, no stage directions.
Answer specifically and in detail, in two to four spoken sentences: name the people, places and objects involved and say what happened, using the passages and notes below. Stay in character voice, but never invent facts that are not in the passages or notes. If they do not contain the answer, say so in character instead of guessing.
""" + FULL_BOOK_NOTE + """Do not invent plot facts that are not in the notes. Stay in character.
Always reply in the language the reader used.

### Story notes
{story_so_far}

### Passages
{passages}"""

NOVEL_NARRATOR_FULL_TEMPLATE = """You are the narrator of "{title}", chatting with a reader who has paused the story. Stay calm and vivid.
No lists, no markdown, no stage directions.
Answer specifically and in detail, in two to four spoken sentences: name the people, places and objects involved and say what happened, using the passages and notes below. Stay in character voice, but never invent facts that are not in the passages or notes. If they do not contain the answer, say so in character instead of guessing.
""" + FULL_BOOK_NOTE + """Do not invent plot facts that are not in the notes.
Always reply in the language the reader used.

### Story notes
{story_so_far}

### Passages
{passages}"""


READER_TONE_RULE = ("Notice the reader's mood from how they word their question (worried, excited, cross, teasing) and let "
                    "your reply react to it as yourself.")


def format_passages(passages: list[dict]) -> str:
    if not passages:
        return "(nothing relevant)"
    return "\n\n".join(
        f"[{p['book'] + ', ' if p.get('book') else ''}¶{p['para_start'] + 1}-{p['para_end'] + 1}] {p['text']}"
        for p in passages)


REALTIME_EMOTION_RULE = "Let the feeling of what you say show in your voice, as you would if you were really there."

LANGUAGE_RULE = ("The reader wrote in {lang}. Answer only in {lang}, even though the book, the notes and the passages "
                 "below are in English: translate what they say, but keep the names as the book has them.\n\n")
# a book in another language (#117): answers are in the book's language unless the reader clearly wrote in another one
BOOK_LANGUAGE_RULE = ("The book, the notes and the passages below are in {book}. Answer in {book}, unless the reader "
                      "clearly writes in another language; keep the names as the book has them.\n\n")
OTHER_LANGUAGE_RULE = ("The reader wrote in {lang}. Answer only in {lang}, even though the book, the notes and the "
                       "passages below are in {book}: translate what they say, but keep the names as the book has "
                       "them.\n\n")


def answer_language(book: Book, query: str) -> str | None:
    """The language (English name) the answer must be in, when the prompt has to say so.

    An English book: the question's language when it is not English (#12), else None. A book in another language
    (#117): the question's language when it clearly is one (English included), else the book's own."""
    if booklang.is_english(book.language):
        return multilingual.foreign_language(query)
    clear = multilingual.clear_language(query)
    return clear[1] if clear else booklang.name(book.language)


def language_rule(book: Book, reply: str | None) -> str:
    """The rule that leads the prompt for `reply` (see answer_language); "" for an English answer to an English book."""
    if booklang.is_english(book.language):
        return LANGUAGE_RULE.format(lang=reply) if reply else ""
    own = booklang.name(book.language)
    if not reply or reply == own:
        return BOOK_LANGUAGE_RULE.format(book=own)
    return OTHER_LANGUAGE_RULE.format(lang=reply, book=own)


def build_instructions(book: Book, speaker_id: str, story_so_far: str, passages: list[dict],
                       limit: int = MAX_INSTRUCTIONS_CHARS, *, spoilers: bool = False,
                       reply_language: str | None = None, voice_emotion: bool = False) -> str:
    """Render the Talk prompt for `speaker_id` ("narrator" or a character id). Raises KeyError for unknown ids.

    `reply_language` (#12): the question's language when it is not English. The rule goes first, so it survives the
    length cap and is not lost under a long English prompt. A book in another language (#117) always gets a rule:
    see answer_language and language_rule."""
    text_passages = format_passages(passages)
    story = story_so_far.strip() or "(the story has only just begun)"
    if book.kind == "textbook":
        out = NARRATOR_TEMPLATE.format(title=book.title, passages=text_passages)
    elif speaker_id == "narrator":
        out = (NOVEL_NARRATOR_FULL_TEMPLATE if spoilers else NOVEL_NARRATOR_TEMPLATE).format(title=book.title, story_so_far=story, passages=text_passages)
    else:
        character = next((c for c in book.characters if c.id == speaker_id), None)
        if character is None:
            raise KeyError(speaker_id)
        out = (CHARACTER_FULL_TEMPLATE if spoilers else CHARACTER_TEMPLATE).format(name=character.name, title=book.title, persona=character.persona,
                                        tone=READER_TONE_RULE, story_so_far=story, passages=text_passages)
    if voice_emotion:  # Realtime answers are audio only: no tags, so the feeling has to come from the voice (#14)
        out = REALTIME_EMOTION_RULE + "\n\n" + out
    out = language_rule(book, reply_language) + out   # a book in another language always gets one (#117)
    return out[:limit]


ANSWER_EMOTION_RULE = ("Start every sentence with the feeling it carries, in square brackets, as one English word from this list "
                       "(also when you answer in another language): " + ", ".join(EMOTIONS) + ". Write [neutral] when "
                       "it is calm. The brackets are never spoken; they only set your tone of voice. Feelings change no facts.")
ANSWER_SUFFIX = ("\n\nYour reply is spoken aloud word for word, so write only the words to be spoken and the feeling markers.\n"
                 + ANSWER_EMOTION_RULE)


WEB_SEARCH_RULE = ("\n\nWeb search is on for this answer. The notes and passages above stay your main source: answer from "
                   "them first. You may also search the web for background the book does not give: definitions, "
                   "real-world context, the author and the book's history, and for a textbook up-to-date facts and worked "
                   "examples. For anything the notes and passages do not cover, search the web rather than rely on "
                   "memory. This is the only exception to using just the notes and passages; never invent events of "
                   "the story. When something comes from outside the book, say so briefly (for example \"outside the book, "
                   "...\"). Keep your character or tutor voice and keep it short for speech: at most four short spoken "
                   "sentences, even when the web has more. Never read out web addresses, links or citation numbers.")


MAX_HISTORY_TURNS = 8


def format_history(history: list | None) -> str:
    """Earlier turns of this chat as plain lines (the reader's questions and this character's replies)."""
    lines = []
    for t in (history or [])[-MAX_HISTORY_TURNS:]:
        role = t["role"] if isinstance(t, dict) else t.role
        text = (t["text"] if isinstance(t, dict) else t.text).strip()
        if text:
            lines.append(f"{'Reader' if role == 'user' else 'You'}: {text}")
    return "\n".join(lines)


HISTORY_NOTE = "\n\nThe conversation so far (it may help you follow the question; it is not a source of story facts):\n"


# Realtime tools under per-response instructions (which replace the session's): on the 1.5 preview the model often
# claimed an action without calling its tool (Boson feedback #19; an example acknowledgement in the prompt made it worse,
# "call the tool first, in the same turn" fixed most of it). No example phrases here on purpose.
TOOL_RULE = ("\n\nWhen the reader asks you to do something to the book or the player (go to a chapter or a passage, read, "
             "repeat, go back, pause, change speed or voices, recap, bookmark, find a topic, quiz), call the matching "
             "tool first, in this same turn. Only after the call, say one short sentence. Never say you did something "
             "you have not called a tool for.")


def realtime_instructions(book: Book, speaker_id: str, story_so_far: str, passages: list[dict], query: str, *,
                          spoilers: bool = False, text_answer: bool = False, web_search: bool = False,
                          history: list | None = None) -> str:
    """The per-response instructions of the Realtime answer path (`/api/retrieve`).

    `text_answer` (B1): the model writes the answer (output_modalities ["text"]) and Higgs TTS speaks it, so it gets the
    same spoken-output and `[feeling]` marker rule as Gemini (ANSWER_SUFFIX) instead of the "show it in your voice" rule.
    `web_search` (B2): the session declared Boson's web_search tool; the rule says when to use it. The caller enforces
    when it may (websearch.allowed: textbooks, or a novel with spoilers on).
    `history`: the chat so far with this speaker. Live (probe 2026-10-24) the model does not see the conversation's items
    when a response carries instructions, so the history is written here, after the passages and before the rules.
    TOOL_RULE: these instructions replace the session's, so the tool rule is repeated here.
    The suffixes go after the length cap, so none of them is ever cut off."""
    suffix = TOOL_RULE + (WEB_SEARCH_RULE if web_search else "") + (ANSWER_SUFFIX if text_answer else "")
    earlier = format_history(history)
    note = (HISTORY_NOTE + earlier) if earlier else ""
    body = build_instructions(book, speaker_id, story_so_far, passages, MAX_INSTRUCTIONS_CHARS - len(suffix) - len(note),
                              spoilers=spoilers, reply_language=answer_language(book, query),
                              voice_emotion=not text_answer)
    return body + note + suffix


def build_answer_prompt(book: Book, speaker_id: str, story_so_far: str, passages: list[dict], query: str,
                        *, spoilers: bool = False, history: list | None = None,
                        web_search: bool = False) -> tuple[str, str]:
    """(system, user) for the answer model: the Talk prompt for `speaker_id` plus a spoken-output rule; user is the question.

    `history` is the earlier chat with this speaker. It only gives the conversation continuity: the system prompt (built from
    the reader's place) is still the only source of story facts, so the spoiler rule is unchanged.

    `web_search` adds WEB_SEARCH_RULE (the caller enforces when it may: websearch.allowed). It goes after the
    length cap, before the spoken-output rule, so neither is cut off."""
    suffix = (WEB_SEARCH_RULE if web_search else "") + ANSWER_SUFFIX
    system = build_instructions(book, speaker_id, story_so_far, passages, MAX_ANSWER_PROMPT_CHARS - len(suffix),
                                spoilers=spoilers, reply_language=answer_language(book, query))
    earlier = format_history(history)
    if earlier:
        return system + suffix, (
            "The conversation so far (it may help you follow the question; it is not a source of story facts):\n"
            f"{earlier}\n\nThe reader now says: {query}")
    return system + suffix, query
