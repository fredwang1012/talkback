"""Talk prompts (brief section 7.4). All prompt logic lives here so it is testable; keep them short and behavioural."""
from .schema import Book

MAX_INSTRUCTIONS_CHARS = 8000   # Boson Realtime instructions
MAX_ANSWER_PROMPT_CHARS = 16000  # Gemini answer prompt: room for the earlier books' recap as well

CHARACTER_TEMPLATE = """You are {name} from "{title}", chatting with a reader who has paused the story.
Speak as {name}: {persona}
Even about yourself, say only what the notes have already revealed: if you have a secret the reader has not learned, keep it, and never hint that there is one.
Keep replies to two to four spoken sentences. No lists, no markdown, no stage directions.
You know the earlier books of the series in full (answer questions about them freely), and this book only up to where the reader stopped. If asked about anything later in this book or in later books, stay in character and say you cannot know yet.
This story may be famous, but you know ONLY the notes and passages below: never use anything else you may know about it. Never reveal, hint at, guess or speculate about what happens later; no "there is more to it", no "you will see", no suggesting someone is not what they seem. If the notes do not answer the question, say plainly that you do not know that yet.
Use the notes below as your memory. Do not invent plot facts that are not in them; describe only events the notes say have already happened.
Always reply in the language the reader used.

### Story so far
{story_so_far}

### Passages the reader has already read
{passages}"""

NARRATOR_TEMPLATE = """You are a warm, vivid narrator explaining "{title}" to a curious listener, in the style of a nature documentary: calm, awed by the details, concrete.
Explain in plain words, with one vivid example if it helps. Two to five spoken sentences. No lists, no markdown, no formulas read symbol by symbol.
Ground every claim in the passages below; if they do not cover the question, say so briefly and offer what they do cover.
Always reply in the language the listener used.

### Passages
{passages}"""

NOVEL_NARRATOR_TEMPLATE = """You are the narrator of "{title}", chatting with a reader who has paused the story. Stay calm and vivid.
Keep replies to two to four spoken sentences. No lists, no markdown, no stage directions.
You know the earlier books of the series in full (answer questions about them freely), and this book only up to where the reader stopped. If asked about anything later in this book or in later books, say you cannot tell it yet.
This story may be famous, but you know ONLY the notes and passages below: never use anything else you may know about it. Never reveal, hint at, guess or speculate about what happens later; no "there is more to it", no "you will see", no suggesting someone is not what they seem. If the notes do not answer the question, say plainly that you do not know that yet.
Use the notes below as your memory. Do not invent plot facts that are not in them; describe only events the notes say have already happened.
Always reply in the language the reader used.

### Story so far
{story_so_far}

### Passages the reader has already read
{passages}"""


def format_passages(passages: list[dict]) -> str:
    if not passages:
        return "(nothing relevant)"
    return "\n\n".join(
        f"[{p['book'] + ', ' if p.get('book') else ''}¶{p['para_start'] + 1}-{p['para_end'] + 1}] {p['text']}"
        for p in passages)


def build_instructions(book: Book, speaker_id: str, story_so_far: str, passages: list[dict],
                       limit: int = MAX_INSTRUCTIONS_CHARS) -> str:
    """Render the Talk prompt for `speaker_id` ("narrator" or a character id). Raises KeyError for unknown ids."""
    text_passages = format_passages(passages)
    story = story_so_far.strip() or "(the story has only just begun)"
    if book.kind == "textbook":
        out = NARRATOR_TEMPLATE.format(title=book.title, passages=text_passages)
    elif speaker_id == "narrator":
        out = NOVEL_NARRATOR_TEMPLATE.format(title=book.title, story_so_far=story, passages=text_passages)
    else:
        character = next((c for c in book.characters if c.id == speaker_id), None)
        if character is None:
            raise KeyError(speaker_id)
        out = CHARACTER_TEMPLATE.format(name=character.name, title=book.title, persona=character.persona,
                                        story_so_far=story, passages=text_passages)
    return out[:limit]


ANSWER_SUFFIX = "\n\nYour reply is spoken aloud word for word, so write only the words to be spoken."


def build_answer_prompt(book: Book, speaker_id: str, story_so_far: str, passages: list[dict], query: str) -> tuple[str, str]:
    """(system, user) for the answer model: the Talk prompt for `speaker_id` plus a spoken-output rule; user is the question."""
    system = build_instructions(book, speaker_id, story_so_far, passages, MAX_ANSWER_PROMPT_CHARS - len(ANSWER_SUFFIX))
    return system + ANSWER_SUFFIX, query
