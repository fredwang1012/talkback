"""#74: verse that is sung in the story gets <|style:singing|>; recited verse and prose do not. Rule-based, strict."""
import pytest

from app.delivery import is_sing_lead_in, sung_from, verse_score
from app.schema import Book, Character, Narrator, Paragraph, Section, Segment
from app.ttschunks import chunk_paragraph

# Real paragraphs from data/books/alice-in-wonderland/book.json (Project Gutenberg, public domain), keyed by idx.
ALICE = {
    39: '"I\'m sure I\'m not Ada," she said, "for her hair goes in such long ringlets, and mine doesn\'t go in ringlets at all; and I\'m sure I can\'t be Mabel, for I know all sorts of things, and she, oh! she knows such a very little! Besides, she\'s she, and I\'m I, and—oh dear, how puzzling it all is! I\'ll try if I know all the things I used to know. Let me see: four times five is twelve, and four times six is thirteen, and four times seven is—oh dear! I shall never get to twenty at that rate! However, the Multiplication Table doesn\'t signify: let\'s try Geography. London is the capital of Paris, and Paris is the capital of Rome, and Rome—no, that\'s all wrong, I\'m certain! I must have been changed for Mabel! I\'ll try and say \'How doth the little—\'" and she crossed her hands on her lap as if she were saying lessons, and began to repeat it, but her voice sounded hoarse and strange, and the words did not come the same as they used to do:—',
    40: '"How doth the little crocodile Improve his shining tail, And pour the waters of the Nile On every golden scale!',
    41: '"How cheerfully he seems to grin, How neatly spread his claws, And welcome little fishes in With gently smiling jaws!"',
    170: '"Repeat, \'You are old, Father William,\'" said the Caterpillar.',
    171: 'Alice folded her hands, and began:—',
    172: '"You are old, Father William," the young man said, "And your hair has become very white; And yet you incessantly stand on your head— Do you think, at your age, it is right?"',
    173: '"In my youth," Father William replied to his son, "I feared it might injure the brain; But, now that I\'m perfectly sure I have none, Why, I do it again and again."',
    258: '"Oh, don\'t bother me," said the Duchess; "I never could abide figures!" And with that she began nursing her child again, singing a sort of lullaby to it as she did so, and giving it a violent shake at the end of every line:',
    259: '"Speak roughly to your little boy, And beat him when he sneezes: He only does it to annoy, Because he knows it teases."',
    260: 'CHORUS. (In which the cook and the baby joined):',
    261: '"Wow! wow! wow!"',
    262: 'While the Duchess sang the second verse of the song, she kept tossing the baby violently up and down, and the poor little thing howled so, that Alice could hardly hear the words:—',
    263: '"I speak severely to my boy, I beat him when he sneezes; For he can thoroughly enjoy The pepper when he pleases!"',
    264: 'CHORUS.',
    265: '"Wow! wow! wow!"',
    266: '"Here! you may nurse it a bit, if you like!" the Duchess said to Alice, flinging the baby at her as she spoke. "I must go and get ready to play croquet with the Queen," and she hurried out of the room. The cook threw a frying-pan after her as she went out, but it just missed her.',
    351: 'The Hatter shook his head mournfully. "Not I!" he replied. "We quarrelled last March—just before he went mad, you know—" (pointing with his tea spoon at the March Hare,) "—it was at the great concert given by the Queen of Hearts, and I had to sing',
    352: "'Twinkle, twinkle, little bat! How I wonder what you're at!'",
    353: 'You know the song, perhaps?"',
    357: 'Here the Dormouse shook itself, and began singing in its sleep "Twinkle, twinkle, twinkle, twinkle—" and went on so long that they had to pinch it to make it stop.',
    592: '"Come, let\'s try the first figure!" said the Mock Turtle to the Gryphon. "We can do without lobsters, you know. Which shall sing?"',
    593: '"Oh, you sing," said the Gryphon. "I\'ve forgotten the words."',
    594: 'So they began solemnly dancing round and round Alice, every now and then treading on her toes when they passed too close, and waving their forepaws to mark the time, while the Mock Turtle sang this, very slowly and sadly:—',
    595: '"Will you walk a little faster?" said a whiting to a snail. "There\'s a porpoise close behind us, and he\'s treading on my tail. See how eagerly the lobsters and the turtles all advance! They are waiting on the shingle—will you come and join the dance? Will you, won\'t you, will you, won\'t you, will you join the dance? Will you, won\'t you, will you, won\'t you, won\'t you join the dance?',
    596: '"You can really have no notion how delightful it will be When they take us up and throw us, with the lobsters, out to sea!" But the snail replied "Too far, too far!" and gave a look askance— Said he thanked the whiting kindly, but he would not join the dance. Would not, could not, would not, could not, would not join the dance. Would not, could not, would not, could not, could not join the dance.',
    597: '"What matters it how far we go?" his scaly friend replied. "There is another shore, you know, upon the other side. The further off from England the nearer is to France— Then turn not pale, beloved snail, but come and join the dance. Will you, won\'t you, will you, won\'t you, will you join the dance? Will you, won\'t you, will you, won\'t you, won\'t you join the dance?"',
    598: '"Thank you, it\'s a very interesting dance to watch," said Alice, feeling very glad that it was over at last: "and I do so like that curious song about the whiting!"',
    628: '"How the creatures order one about, and make one repeat lessons!" thought Alice; "I might as well be at school at once." However, she got up, and began to repeat it, but her head was so full of the Lobster Quadrille, that she hardly knew what she was saying, and the words came very queer indeed:—',
    629: '"\'Tis the voice of the Lobster; I heard him declare, "You have baked me too brown, I must sugar my hair." As a duck with its eyelids, so he with his nose Trims his belt and his buttons, and turns out his toes."',
    630: '[later editions continued as follows When the sands are all dry, he is gay as a lark, And will talk in contemptuous tones of the Shark, But, when the tide rises and sharks are around, His voice has a timid and tremulous sound.]',
    645: '"Oh, a song, please, if the Mock Turtle would be so kind," Alice replied, so eagerly that the Gryphon said, in a rather offended tone, "Hm! No accounting for tastes! Sing her \'Turtle Soup,\' will you, old fellow?"',
    646: 'The Mock Turtle sighed deeply, and began, in a voice sometimes choked with sobs, to sing this:—',
    647: '"Beautiful Soup, so rich and green, Waiting in a hot tureen! Who for such dainties would not stoop? Soup of the evening, beautiful Soup! Soup of the evening, beautiful Soup! Beau—ootiful Soo—oop! Beau—ootiful Soo—oop! Soo—oop of the e—e—evening, Beautiful, beautiful Soup!',
    648: '"Beautiful Soup! Who cares for fish, Game, or any other dish? Who would not give all else for two p ennyworth only of beautiful Soup? Pennyworth only of beautiful Soup? Beau—ootiful Soo—oop! Beau—ootiful Soo—oop! Soo—oop of the e—e—evening, Beautiful, beauti—FUL SOUP!"',
    649: '"Chorus again!" cried the Gryphon, and the Mock Turtle had just begun to repeat it, when a cry of "The trial\'s beginning!" was heard in the distance.',
}


def sung(*idxs):
    """sung_from for each paragraph of an Alice run, each judged with the run before it as context."""
    texts = [ALICE[i] for i in idxs]
    return [sung_from(texts[:k], texts[k]) for k in range(len(texts))]


# ---------- detection on Alice ----------

@pytest.mark.parametrize("run,expected", [
    ((258, 259), [None, 0]),  # the Duchess "singing a sort of lullaby ... every line:" -> first verse
    ((260, 261), [None, 0]),  # "CHORUS. (In which the cook and the baby joined):" -> "Wow! wow! wow!"
    ((262, 263, 264, 265, 266), [None, 0, None, 0, None]),  # second verse; "CHORUS." heading; then prose again
    ((594, 595, 596, 597, 598), [None, 0, 0, 0, None]),  # Lobster Quadrille: quote stays open over 3 paragraphs
    ((646, 647, 648, 649), [None, 0, 0, None]),  # Beautiful Soup, two verses, then the Gryphon cries "Chorus again!"
    ((351, 352, 353), [None, 0, None]),  # Hatter: "...and I had to sing" -> 'Twinkle, twinkle, little bat!'
])
def test_sung_verse_in_alice(run, expected):
    assert sung(*run) == expected


@pytest.mark.parametrize("run", [
    (39, 40, 41),  # "How doth the little crocodile": Alice "began to repeat it"
    (170, 171, 172, 173),  # "You are old, Father William": recited ("Alice folded her hands, and began:")
    (628, 629, 630),  # "'Tis the voice of the Lobster": "began to repeat it"
    (592, 593, 594),  # "Which shall sing?" / "Oh, you sing," - talk about singing is not a song
    (645, 646),  # "Sing her 'Turtle Soup'": the lead-in itself is not sung
    (357,),  # Dormouse "began singing in its sleep "Twinkle..." and went on": narration resumes, not sung
])
def test_recited_verse_and_prose_are_not_sung(run):
    assert all(s is None for s in sung(*run))


def test_lead_in_rules():
    assert is_sing_lead_in(ALICE[594]) and is_sing_lead_in(ALICE[646]) and is_sing_lead_in(ALICE[258])
    assert is_sing_lead_in(ALICE[351])  # paragraph ends on the verb: "...and I had to sing"
    assert is_sing_lead_in("CHORUS.")
    assert not is_sing_lead_in(ALICE[39])  # repeat
    assert not is_sing_lead_in("He pocketed the flute. He didn't feel much like singing:")
    assert not is_sing_lead_in('"Ready?" Hermione sang out:')  # 'sang out' = called out
    assert not is_sing_lead_in("She spoke in a sing-song voice:")
    assert not is_sing_lead_in("They sang all the way home.")  # no colon: the song is not quoted next
    assert not is_sing_lead_in("She sat down and began to repeat the song she had learned:")


def test_verse_score_counts_line_starts_not_names_or_dialogue():
    assert verse_score(ALICE[40]) >= 2 and verse_score(ALICE[259]) >= 2
    assert verse_score(ALICE[266]) == 0  # prose dialogue
    assert verse_score('"Yes," said Harry, "And then Ron left."') == 0  # capital after an opening quote
    assert verse_score("They talked about You-Know-Who and Ron and Hermione at the Burrow.") == 0


# ---------- synthetic: inline lead-in, false positives, caps ----------

HAT = ('For a few seconds, there was complete silence. Then the hat twitched, and it began to sing: '
       '"Oh, I am old and I am green, But wiser than I seem, I see inside each hidden head And every hidden dream.')
STANZA = "So sit me down upon your head, And hear what I can say, For I will tell you where you go And then be on your way."


def test_inline_lead_in_sings_from_the_opening_quote():
    assert sung_from([], HAT) == HAT.index('"Oh,')
    assert sung_from([HAT], STANZA) == 0  # an unquoted stanza that reads as verse continues the song
    assert sung_from([HAT, STANZA], "The whole hall burst into applause as the hat finished its song.") is None


def test_lead_in_then_prose_or_dialogue_is_not_sung():
    lead = "Then she began to sing:"
    assert sung_from([lead], '"La la," she said, and walked away down the long road home, quite cross.') is None
    assert sung_from([lead], "Nobody listened, of course, and the Rat went on rowing as before.") is None  # no quote
    assert sung_from([], 'He began to sing: "La la," he said, and stopped to look at the river.') is None
    assert sung_from(["He didn't feel much like singing:"], '"Oh, the river is wide, And the water is deep."') is None
    # a bare dash after 'singing' is commentary going on, not a song (HP5 Quidditch shape, invented text)
    assert sung_from([], 'Harry heard the crowd, all yelling and booing and singing — "— passes to Bell, and the '
                         'crowd love this, what is that they are singing?"') is None
    assert not is_sing_lead_in("the crowd were all yelling and singing —")


def test_caps_on_paragraph_length_and_block_length():
    lead = "And the Rat began to sing:"
    verse = '"Oh, the river runs, And the reeds are green, And the water flows Where the boats have been.'
    assert sung_from([lead], '"' + "Oh the river runs, And the reeds are green, " * 40) is None  # far too long
    run = [lead] + [verse] * 12
    results = [sung_from(run[:k], run[k]) for k in range(1, len(run))]
    assert results[0] == 0 and None in results  # a song is cut off after a few paragraphs
    assert all(r is None for r in results[results.index(None):])


# ---------- tts_text ----------

def book_of(*paras, voice_tags="<|prosody:speed_slow|>", section_starts=(0,)):
    """paras: (text, segments) pairs; character 'turtle' has voice tags."""
    book = Book(id="b", title="B", author="A", license="PD", kind="novel", narrator=Narrator(voice="nv"),
                characters=[Character(id="turtle", name="Mock Turtle", voice="tv", voice_tags=voice_tags,
                                      first_para=0, persona="p")],
                sections=[Section(id=f"s{i}", title="S", start_para=i) for i in section_starts],
                paragraphs=[])
    for i, (t, ss) in enumerate(paras):
        book.paragraphs.append(Paragraph(idx=i, section_id="s0", text=t, segments=ss))
    return book


def test_sung_chunk_tag_order_and_no_sfx():
    song = '"Ha ha! Beautiful Soup, so rich and green, Waiting in a hot tureen!"'
    book = book_of(("CHORUS.", [Segment(speaker="narrator", text="CHORUS.")]),
                   (song, [Segment(speaker="turtle", text=song, emotion="longing")]))
    chunks = chunk_paragraph(book.paragraphs[1], book)
    # voice tags, style, emotion, (no expressive_*), pause, text; no sfx:laughter before 'Ha ha!' when sung
    assert chunks[0].tts_text == ('<|prosody:speed_slow|><|style:singing|><|emotion:longing|><|prosody:pause|>'
                                  + song)
    unsung = book_of((song, [Segment(speaker="turtle", text=song)]), section_starts=(5,))
    assert "<|sfx:laughter|>" in chunk_paragraph(unsung.paragraphs[0], unsung)[0].tts_text  # sanity: sfx otherwise


def test_singing_overrides_whisper_or_shout_style():
    song = '"Soo-oop of the e-e-evening, Beautiful, beautiful Soup!"'
    book = book_of((ALICE[646], [Segment(speaker="narrator", text=ALICE[646])]),
                   (song, [Segment(speaker="turtle", text=song, style="shouting")]))
    t = chunk_paragraph(book.paragraphs[1], book)[0].tts_text
    assert t.count("<|style:") == 1 and "<|style:singing|>" in t and "expressive" not in t


def test_inline_song_splits_the_segment_at_the_quote():
    book = book_of((HAT, [Segment(speaker="narrator", text=HAT)]), section_starts=(5,))
    chunks = chunk_paragraph(book.paragraphs[0], book)
    q = HAT.index('"Oh,')
    assert [c.char_start for c in chunks if "<|style:singing|>" in c.tts_text] == [q]
    assert all("singing" not in c.tts_text for c in chunks if c.char_start < q)
    assert chunks[-1].char_end == len(HAT) and HAT[chunks[-2].char_start:chunks[-2].char_end].endswith("sing:")


def test_prose_and_legacy_chunks_never_sing():
    book = book_of((ALICE[646], [Segment(speaker="narrator", text=ALICE[646])]),
                   (ALICE[647], [Segment(speaker="turtle", text=ALICE[647])]))
    assert "singing" not in chunk_paragraph(book.paragraphs[0], book)[0].tts_text
    assert all("singing" in c.tts_text for c in chunk_paragraph(book.paragraphs[1], book))
    assert all("singing" not in c.tts_text for c in chunk_paragraph(book.paragraphs[1], book, expressive=False))
