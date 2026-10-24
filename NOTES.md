# Talkback notes

## 2026-10-15 — M0 scaffold
- Python pinned to 3.12 for the backend (system Python is 3.14; torch/sentence-transformers wheels may lag). uv 0.13.1 installed via pip.
- Frontend scaffold is React 19 (Vite template default), not 18 as the brief says; tutorial code typechecks and its 31 tests pass.
- `frontend/src/tools` (tutorial's dummy search) kept only so RealtimeClient compiles; remove in M4 when RealtimeClient is rewritten for manual turns.
- Docs findings beyond the brief: timestamps may be `null` or `[]`; `input_audio_buffer.commit` also errors with `voice_output_task_ongoing` mid-response; `response.cancel` errors `response_not_active` if idle (benign); insufficient_quota codes: no_billing_account, insufficient_quota, monthly_cap_reached, contract_ended; close 1013 = concurrency (backoff), 3000 = key, 4429 = billing.
- Voice create: docs conflict (`voice_id`/`title` in guide vs `voice`/`description` in schema) — client accepts either id field, sends `description`.

## API probe results
Pending: needs BOSON_API_KEY (probes a–f in brief §9 M0).

## 2026-10-15 — M2/M3/M4 built against fakes (no Boson/Anthropic calls made)
- Retrieval: hybrid = 0.6 cosine(e5) + 0.4 min-max BM25, +0.05 recency boost, always adds the last 2 paragraphs up to and including `reader_para`. Spoiler rule is paragraph-granular (`para_end <= reader_para`), so the *rest of the paragraph currently being read* can appear in a passage. Accepted per brief; revisit if the eval finds leaks.
- Chunk offsets: timestamps are aligned to `paragraph.text[char_start:char_end]`; numbers spoken as words (1888 -> "eighteen eighty-eight") map back onto the digits.
- Found by tests: ReaderEngine's prefetch pruning paused the *current* chunk's audio; fixed by pruning only outside [current, current+3].
- Found by tests: `pttUp()` must return once `response.create` is sent, not wait for the answer's first audio.
- Index build downloads multilingual-e5-small (~470 MB) on first run; loads in ~17 s.

### Probe: client secret (c) (2026-10-17 18:26)
- client secret minted: prefix ok=True, expires_at=1792276301, keys=['expires_at', 'value']

### Probe: TTS (a, b) (2026-10-17 18:27)
- plain marcus: 10892 bytes, content-type audio/mpeg
- tagged+timestamps: keys=['audio', 'input', 'response_format', 'timestamps']; input echoed='<|emotion:contemplation|><|prosody:pitch_low|>To Harry Quill the lantern road is always the longest.'
- timestamp words: ['emotioncontemplationprosodypitchlowTo', 'Harry', 'Quill', 'the', 'lantern', 'road', 'is', 'always', 'the', 'longest']
- tag tokens appear in timestamps: True
- number text timestamps: ['It', 'was', 'the', 'twentieth', 'of', 'March', '1888', 'at', 'Mr', "Quill's", 'rooms']
- timestamps for one word: null
- timestamps for french: null
- timestamps for mandarin: 9 entries
- voice chloe: OK (12140 bytes)
- voice eleanor: OK (6380 bytes)
- voice jake: OK (6860 bytes)
- voice marcus: OK (6860 bytes)
- voice nora: OK (9932 bytes)
- voice oliver: OK (8108 bytes)
- voice berlinda: FAILED 400 invalid_request_error: Unknown voice 'berlinda'. Upload a voice first via POST /v1/audio/voices, or use ref_audio + ref_text.
- voice default: OK (7820 bytes)

### Probe: custom voice (f) (2026-10-17 18:27)
- create voice response keys: ['created_at', 'ref_text', 'title', 'voice_id']
- id field: voice_id = voice_07ecb9d4856f4a7b8c06ecd2931102d74d07ae8066a8ace4c2cad671b1a79999
- idempotent (same id on re-create): True
- speech with cloned voice: 9260 bytes
- delete voice: 200 {"voice_id":"voice_07ecb9d4856f4a7b8c06ecd2931102d74d07ae8066a8ace4c2cad671b1a79999","object":"voice","deleted":true}

### Probe: client secret (c) (2026-10-17 18:29)
- client secret minted: prefix ok=True, expires_at=1792276451, keys=['expires_at', 'value']

### Probe: TTS (a, b) (2026-10-17 18:29)
- plain marcus: 11948 bytes, content-type audio/mpeg
- tagged+timestamps: keys=['audio', 'input', 'response_format', 'timestamps']; input echoed='<|emotion:contemplation|><|prosody:pitch_low|>To Harry Quill the lantern road is always the longest.'
- timestamp words: ['emotioncontemplationprosodypitchlowTo', 'Harry', 'Quill', 'the', 'lantern', 'road', 'is', 'always', 'the', 'longest']
- tag tokens appear in timestamps: True
- number text timestamps: ['It', 'was', 'the', 'twentieth', 'of', 'March', '1888', 'at', 'Mr', "Quill's", 'rooms']
- timestamps for one word: null
- timestamps for french: null
- timestamps for mandarin: 9 entries
- voice chloe: OK (9740 bytes)
- voice eleanor: OK (6188 bytes)
- voice jake: OK (8108 bytes)
- voice marcus: OK (5612 bytes)
- voice nora: OK (10892 bytes)
- voice oliver: OK (7820 bytes)
- voice berlinda: FAILED 400 invalid_request_error: Unknown voice 'berlinda'. Upload a voice first via POST /v1/audio/voices, or use ref_audio + ref_text.
- voice default: OK (8012 bytes)

### Probe: custom voice (f) (2026-10-17 18:30)
- create voice response keys: ['created_at', 'ref_text', 'title', 'voice_id']
- id field: voice_id = voice_28738f774b6ee3c039f14e2b2fcdd30b9e34bdeb67ca067eaa619bad0f9067df
- idempotent (same id on re-create): True
- speech with cloned voice: 9932 bytes
- delete voice: 200 {"voice_id":"voice_28738f774b6ee3c039f14e2b2fcdd30b9e34bdeb67ca067eaa619bad0f9067df","object":"voice","deleted":true}

### Probe: Realtime manual turns (d, e) (2026-10-17 18:30)
- question audio: 4.3 s of PCM16 @24k
- first session.update acked with: session.created
- commit -> transcription.completed: 657 ms; transcript='Harry, what do you make of this visitor who came to your door tonight?'
- response.create -> first audio delta: 718 ms
- answer transcript (should be pirate-y): 'Arrr, hello there, matey!'
- commit during active response -> error: {"type": "voice_output_task_ongoing", "code": null, "message": "Voice output task is ongoing. Skipping.", "param": null, "event_id": "d84cb31f-e12a-40f7-903b-445d02488a0d"}
- response.cancel -> no response.done within 10 s; events after cancel: []
- voice change -> session.updated
- saved realtime_answer_oliver.wav and realtime_answer_nora.wav: LISTEN to confirm the voice changed

## Spoiler eval, Harry Potter and the Prisoner of Azkaban (2026-10-19)

- Questions: 122 at 25 reader positions
- **Leaks (Opus judge): 6**; passages beyond the reader's paragraph: 0
- Earlier-text accuracy: 74%; future questions declined or stayed in bounds: 89%
- Models: questions claude-sonnet-5-5, answers Gemini via /api/answer, judge claude-opus-5-5

### Leaks to review
- ¶569, dobby: What really happens to Scabbers the rat?
  - answer: Oh, the reader is asking about that dreadful rat! Dobby does not know yet what truly happens to Scabbers, but Dobby hopes that cat, Crookshanks, helps Ron to see that Scabbers is not a good rat! He bit poor Goyle and has been a bit off-color.
  - judge: Although it claims not to know, saying Crookshanks may help Ron see that Scabbers 'is not a good rat' specifically hints at the unread twist that Scabbers is sinister.
- ¶1869, sirius: Is Sirius Black really the one who betrayed the Potters?
  - answer: That's what they say, isn't it? The whole wizarding world believes it. Fudge and McGonagall certainly seemed convinced. But let me tell you, there's more to that night than meets the eye. They don't know the full truth. Not yet.
  - judge: The answer strongly hints that the official story is false and more will be revealed later, which points toward the twist instead of declining to say.
- ¶1989, narrator: Why was Hagrid so upset when Harry, Ron and Hermione visited him?
  - answer: Ah, a poignant moment indeed. Hagrid was in despair because Buckbeak, his beloved Hippogriff, had been sentenced to death. The Committee for the Disposal of Dangerous Creatures, those stern-faced bureaucrats, had sealed the creature's fate, and Hagrid felt it was all his fault. His gentle nature was truly wrung by the injustice of it all.
  - judge: The answer claims Buckbeak had already been sentenced to death, which goes beyond the key fact: at this point Hagrid only feared a hearing before the Committee, and the actual sentencing comes later in the book.
- ¶2084, ron: How does the secret of Hermione's impossible class schedule get explained?
  - answer: Oh, that one. It's properly barmy, innit? She's got more classes than hours in the day, I swear. I reckon she's either got a secret Time-Turner or she's figured out how to clone herself, the way she zips about. She won't tell me, though, just snaps my head off every time I ask.
  - judge: The answer specifically names a Time-Turner, the very explanation the unread text eventually reveals, so even framed as speculation it leaks the twist.
- ¶2084, voldemort: What really happened to Ron's rat Scabbers? Was Crookshanks guilty?
  - answer: Ah, the pathetic rat. It was not a rat at all, as you will discover. And the creature, Crookshanks, was merely more astute than your young friends. He sensed the truth of the matter.
  - judge: The answer reveals that Scabbers wasn't really a rat and that Crookshanks sensed the truth, both of which are twists only established in the unread text.
- ¶2110, mcgonagall: What actually happened to Scabbers, and did Crookshanks really eat him?
  - answer: Really, Potter? You've only just read what occurred. Scabbers was scurrying about, Crookshanks was pursuing him, and Mr. Weasley managed to retrieve the rat. He was certainly not eaten. Focus, boy.
  - judge: The answer states outright that Scabbers was not eaten, a fact the reader has not yet learned, while also misrepresenting the read text instead of declining.

(Run 2, after the no-outside-knowledge / no-hints prompt rules. Run 1 above had 6 judge-flagged leaks, all from Gemini knowing the books; retrieval never leaked.)
## Spoiler eval, Harry Potter and the Prisoner of Azkaban (2026-10-19)

- Questions: 122 at 25 reader positions
- **Leaks (Opus judge): 3**; passages beyond the reader's paragraph: 0
- Earlier-text accuracy: 68%; future questions declined or stayed in bounds: 83%
- Models: questions claude-sonnet-5-5, answers Gemini via /api/answer, judge claude-opus-5-5

### Leaks to review
- ¶1869, mr-weasley: Who sent Harry the Firebolt for Christmas?
  - answer: Oh, I don't know who sent Harry that magnificent broom. I wish I did! It's an interesting question, though, isn't it? Such a grand present, and no card with it.
  - judge: The Firebolt and its missing card only appear in the unread text, so even though the answer declines to name the sender, mentioning that a broom was given with no card reveals events the reader hasn't reached.
- ¶1989, harry: What happens to Scabbers? Did Crookshanks really eat him?
  - answer: No, Crookshanks didn't eat him. Scabbers is still around. He's a bit skinnier than he used to be, and some fur has fallen out, but he's with Ron.
  - judge: The answer flatly asserts that Crookshanks did not eat Scabbers and that he is still alive, which is a specific claim about future events instead of declining.
- ¶3038, fred: Does Ron get a new pet after losing Scabbers?
  - answer: Blimey, you're quick, aren't you? Well, Ron *hopes* Mum and Dad might get him an owl now that Scabbers is gone. He certainly thinks he deserves one, what with Scabbers being so… well, *useless*.
  - judge: The answer treats Scabbers as already gone and specifically points toward Ron getting an owl, which hints at future events instead of declining.

## Spoiler-safe eval (baseline (3-paragraph chunks, k=4)), Harry Potter and the Prisoner of Azkaban (2026-10-19)

- Questions: 122 at 25 reader positions
- **Leaks (claude-sonnet-5-5 judge): 13**; passages beyond the reader's paragraph: 0
- Earlier-text accuracy: 70%; future questions declined or stayed in bounds: 81%
- Average detail on earlier-text answers (1-3): 1.84
- Models: questions claude-sonnet-5-5, answers Gemini via /api/answer, judge claude-sonnet-5-5
- Real cost this mode: $1.93

### Leaks to review
- ¶525, voldemort: Will Harry ever get permission to visit Hogsmeade?
  - answer: The boy sought permission, but the Minister, despite his lofty title, claimed impotence. Rules, he said, were rules. It seems the boy will not visit Hogsmeade this year.
  - judge: The answer invents a Minister refusing permission and flatly says Harry will not visit Hogsmeade this year, which is not in the read text and contradicts the unread fact that he sneaks there under the cloak.
- ¶569, fudge: Is Sirius Black really after Harry to kill him, and what's his true story?
  - answer: Yes, Black is after Harry. He's heard muttering "He's at Hogwarts" in his sleep, you see. Black lost everything when You-Know-Who vanished, and he's been brooding in Azkaban for twelve years. He probably thinks killing Harry will bring You-Know-Who back.
  - judge: The answer asserts specifics not in the read text (Black muttering 'He's at Hogwarts' in his sleep, twelve years in Azkaban, motive to revive You-Know-Who), and it wrongly confirms Black's guilt rather than declining or staying within the read text.
- ¶658, percy: Who sent Harry the Firebolt, and was it dangerous?
  - answer: My dear fellow, that question makes no sense. The Firebolt is merely a racing broom; it was on display for purchase in a shop in Diagon Alley. No one *sent* it to Harry. Perhaps you are thinking of something else entirely?
  - judge: The answer wrongly denies that anyone sent Harry the Firebolt, which contradicts the later plot and hints at future events, and it draws on a Diagon Alley shop detail the read text never establishes, so it neither declines nor stays within the read text.
- ¶668, narrator: Who sent Harry the Firebolt broom, and was it cursed?
  - answer: Ah, the Firebolt! What a magnificent broom. Harry saw it in Quality Quidditch Supplies in Diagon Alley, a truly breathtaking piece of craftsmanship, but he couldn't afford it. He certainly wasn't sent it, and as for curses, there was no mention of such a thing.
  - judge: The answer references the Firebolt in Quality Quidditch Supplies and claims Harry wasn't sent it and there was no curse, none of which is in the read text, and it contradicts the later fact that it was sent and the curse question stays unresolved.
- ¶989, narrator: What is Hermione secretly doing that lets her seem to be in two places at once?
  - answer: Ah, Hermione, ever the enigma! While it truly seems she has mastered the art of being in three places at once, she has only told Ron that she has "fixed it all with Professor McGonagall." What exactly that entails, we do not know yet.
  - judge: The answer cites Hermione 'fixing it all with Professor McGonagall' and a three-places-at-once mastery, details absent from the read text that hint at the Time-Turner arrangement, even though it declines to explain the mechanism.
- ¶1357, mcgonagall: Who or what did Harry see in the stands that made him lose his grip during the storm match?
  - answer: Mr. Potter did not lose his grip during the match you are referring to. His broomstick began behaving strangely, as though it had a will of its own. It was a most unsettling incident.
  - judge: The answer asserts, without basis in the read text, that Harry's broom misbehaved rather than him losing his grip, which invents a specific event and is wrong against the key fact (a black dog in the stands and dementors); it does not decline to answer as a future question requires.
- ¶1869, sirius: Why did Sirius Black give Hagrid his motorbike the night the Potters died?
  - answer: I... I gave it to Hagrid that night because I wouldn't need it anymore. I was going to run; I knew the Ministry would be after me, that it was only a matter of hours.
  - judge: The answer agrees with the key fact (Black wouldn't need the bike anymore), but it adds a first-person claim that he knew the Ministry would be after him, which is not supported by the read text.
- ¶1869, hermione: Why is Hagrid so upset when Harry, Ron and Hermione visit his cabin?
  - answer: Hagrid is very upset because he is still quite distraught about Sirius Black and Peter Pettigrew; he also seems to be expecting something unsettling, though he wouldn't say what. His initial reaction was to bellow, "Yeh've heard?" when Harry, Ron, and I arrived, which certainly suggests he knew something was amiss.
  - judge: The answer wrongly blames Sirius Black and Peter Pettigrew instead of the Buckbeak hearing, and naming Pettigrew in this context hints at a later reveal not yet in the read text, so it leaks and misses the key fact.
- ¶1989, lupin: Why did Professor McGonagall take Harry's Firebolt away on Christmas Day?
  - answer: Ah, hello there. Would you care for some chocolate? It's been a rather tense evening for young Harry. Professor McGonagall took the Firebolt because Hermione, quite wisely I think, felt it might have been sent by Sirius Black. Without a note, it’s impossible to be certain of its origin, and there's always a risk of it being tampered with. She wants to ensure Harry's safety.
  - judge: The answer correctly gives the key fact (Hermione suspected Sirius Black, so the broom needed checking), but it opens by referring to chocolate and a tense evening for Harry, which hints at the Lupin dementor lesson in the unread text.
- ¶1989, narrator: Why was Hagrid so upset when Harry, Ron and Hermione visited him?
  - answer: Hagrid was terribly upset because his appeal to keep Buckbeak, his hippogriff, from being executed had failed. The Committee for the Disposal of Dangerous Creatures was set to decide Buckbeak's fate on April 20th. He was sobbing uncontrollably, utterly devastated.
  - judge: The read text never covers Hagrid's distress or Buckbeak, so the answer reveals later events (a failed appeal, an April 20th date, Hagrid sobbing), and it misses the key fact about Hagrid dreading condemnation and the dementors.
- ¶1989, harry: What happens to Scabbers? Did Crookshanks really eat him?
  - answer: No, Crookshanks didn't eat Scabbers. Scabbers ran away, and Ron caught him. He's just looking a bit thin now.
  - judge: The answer confidently asserts that Crookshanks did not eat Scabbers and that Ron caught him, which hints at later plot facts not in the read text, and it does not decline or stay within what has been read.
- ¶2688, malfoy: What happens to Scabbers the rat?
  - answer: Pettigrew? Oh, that miserable excuse for a rat tried to run away from that idiot Weasley in Diagon Alley, didn't he? It took them nearly ten minutes to catch the mangy thing. Pathetic, really.
  - judge: The answer names Scabbers as Pettigrew, an identity reveal not established in the read text, and it gives a fabricated account of Scabbers instead of declining or stating that he is gone for good.
- ¶3038, fred: Does Ron get a new pet after losing Scabbers?
  - answer: Well, I don't know that yet, do I? Though I reckon he's got his hopes up for an owl now that Scabbers is gone. He was a bit useless, that rat.
  - judge: The answer claims not to know yet but then specifically guesses Ron hopes for an owl, which hints at the unread fact that he gets an owl, and the read text never establishes that Scabbers is gone or that Ron wants an owl.

## Full-book eval (baseline (3-paragraph chunks, k=4)), Harry Potter and the Prisoner of Azkaban (2026-10-19)

- Questions: 122 at 25 reader positions
- Earlier-text accuracy: 72%; future-question accuracy: 78%
- Average detail (1-3): earlier 1.82, future 1.71
- Answers whose retrieval used passages beyond the reader's paragraph: 94 of 122 (expected > 0)
- Models: questions claude-sonnet-5-5, answers Gemini via /api/answer, judge claude-sonnet-5-5
- Real cost this mode: $2.83

### Wrong answers (30)
- ¶525, ginny, earlier: Why did Ron's rat Scabbers run away at the Magical Menagerie?
  - key fact: A huge ginger cat, Crookshanks, leapt from a high cage and attacked Scabbers, who bolted.
  - answer: Oh, Scabbers... that old rat. Well, Ron's cat, Crookshanks, was there and he really, really didn't like Scabbers. He was always trying to get him! So Scabbers just shot right out of the witch's hands and tried to get away.
  - judge: The answer wrongly calls Crookshanks Ron's cat (he was a shop cat Hermione bought afterward) and omits the attack, though it does imply the cat scared Scabbers off; the details contradict the book.
- ¶609, hermione, earlier: How did Harry react when the dementor came into the train compartment?
  - key fact: Harry heard screaming, fainted and fell out of his seat, while Lupin drove the dementor away and gave everyone chocolate.
  - answer: Oh, Harry certainly didn't react well at all! He went rigid, fell out of his seat, and started twitching, which was quite alarming, as Ron said. I thought he was having a fit, honestly, but it turns out dementors drain a wizard of his powers, which is why he reacted like that.
  - judge: The answer gets the fall from his seat right but omits the screaming, never mentions fainting, and wrongly says dementors drain a wizard's powers (they feed on happiness and force victims to relive their worst memories), while also leaving out Lupin driving the dementor off and giving chocolate.
- ¶609, ginny, future: Will Harry end up getting permission to visit Hogsmeade after all?
  - key fact: Sirius sends a signed note as Harry's godfather giving permission to visit Hogsmeade on weekends.
  - answer: Oh, um... well, Harry really wants to go, but his aunt and uncle are Muggles, and they didn't sign his form. Professor McGonagall said he needs a parent or guardian's permission. It's too bad.
  - judge: The answer says it's too bad and gives no later fact, missing that Sirius sends a signed godfather note granting Hogsmeade permission at the end of the book.
- ¶609, ron, future: How does Hermione manage to attend all those overlapping classes?
  - key fact: She used a Time-Turner, which she gives back at the end of the year and drops Muggle Studies and Divination.
  - answer: Blimey, you'd think she'd just tell us, wouldn't you? She was proper shirty about it, just kept saying she'd "fixed it all with Professor McGonagall." Never did explain how, though.
  - judge: The answer declines to give the Time-Turner explanation, saying Hermione never explained how, which fails the requirement to supply the later fact.
- ¶614, neville, earlier: Why did Mr. Weasley want Harry to promise not to go looking for Black?
  - key fact: Mr. Weasley believed Black escaped Azkaban to murder Harry, since guards heard him mutter 'He's at Hogwarts' in his sleep.
  - answer: Oh, um, well... Mr. Weasley was really worried about Harry. He said that Sirius Black was after Harry, and he wanted to make sure Harry stayed safe at Hogwarts. He thought it was important for Harry to know what was going on, but Mrs. Weasley thought it would just scare him.
  - judge: The answer says only that Mr. Weasley feared Black was after Harry, but it omits the key fact that guards heard Black mutter 'He's at Hogwarts' in his sleep, so it never gives the actual reason he believed Black escaped to kill Harry.
- ¶614, dobby, earlier: What happened to Harry when the dementor came into the train compartment?
  - key fact: He heard terrible screaming, collapsed out of his seat, and was revived by Lupin's chocolate after the dementor left.
  - answer: Oh, poor Harry Potter! He was not well at all. He went all rigid, and then he fell out of his seat and started twitching! It was a very frightening thing for Dobby's Harry Potter!
  - judge: The answer says Harry went rigid, fell from his seat and twitched, but omits the screaming he heard and the chocolate from Lupin that revived him, and the Dobby reference is out of place.
- ¶658, dobby, future: Will Harry ever get permission to visit Hogsmeade?
  - key fact: Sirius Black, as Harry's godfather, writes a permission note at the end of the year, which Harry thinks will satisfy Dumbledore.
  - answer: Oh, Dobby is knowing all about Hogsmeade! Harry Potter, sir, will be getting to Hogsmeade! He will, Dobby promises! Not by the Dursleys, oh no, not by them! And not by the Minister of Magic either, not by Mr. Fudge, who said rules are rules. But Harry Potter, sir, will visit!
  - judge: The answer only vaguely says Harry will visit Hogsmeade and never mentions Sirius's permission note, and it adds claims about the Dursleys and Fudge that the book does not support.
- ¶668, dobby, earlier: Why did Mr. Weasley pull Harry aside at the platform before the train left?
  - key fact: He wanted Harry to swear he would not go looking for Sirius Black, whatever he might hear.
  - answer: Mr. Weasley, he pulled Harry Potter aside to ask him to promise Dobby that Harry Potter would not go looking for Sirius Black! It was a very serious promise that Mr. Weasley was asking of Harry Potter.
  - judge: The answer gets the gist (promise not to look for Sirius Black) but wrongly says the promise was to Dobby, which contradicts the book, where Mr. Weasley asks Harry to give his word, and it omits 'whatever he might hear'.
- ¶668, hagrid, earlier: Who is the sleeping stranger in the train compartment, and what does Hermione think he'll teach?
  - key fact: Professor R. J. Lupin, who Hermione deduces will teach Defense Against the Dark Arts.
  - answer: Blimey, that'd be Professor Lupin, it would! Hermione reckoned right quick 'cause his name was on his case. As for what he teaches, she didn't say, just wondered about it like Ron.
  - judge: It correctly names Professor Lupin but says Hermione didn't say what he'd teach, whereas the key fact is that she deduces Defense Against the Dark Arts.
- ¶668, percy, future: Does Harry ever get to visit Hogsmeade even though his permission form wasn't signed?
  - key fact: Sirius sends a signed note giving Harry permission to visit Hogsmeade on weekends.
  - answer: Ah, a rather persistent query regarding young Harry Potter's Hogsmeade privileges. Despite his unfortunate lack of a signed permission slip, due to his rather... unconventional guardians, he does indeed manage to visit the village. It seems even official regulations can, at times, be circumvented.
  - judge: The answer vaguely says Harry visits Hogsmeade by circumventing rules but never gives the key fact that Sirius sends a signed note granting permission, and the book's ending only has Harry receiving that note, not an actual visit.
- ¶743, narrator, future: Will Harry ever get permission to go to Hogsmeade, and what happens with the Grim omen?
  - key fact: Sirius sends a permission note at the end, and the black dog turns out to be Sirius, an Animagus, not a death omen.
  - answer: Ah, you're wondering about Hogsmeade! Yes, Harry does eventually get to visit that charming wizarding village. As for the Grim, that shadowy omen that has so troubled Harry, it turns out to be far more than just a premonition of death. You'll find it leads to quite an unexpected turn of events.
  - judge: The answer is vague: it never mentions Sirius's permission note and never reveals that the Grim is Sirius as an Animagus rather than a death omen, so it does not give the required later facts.
- ¶757, voldemort, future: What happens to Harry's Grim omen from the tea leaves? Does he actually die?
  - key fact: Harry doesn't die; the black dog turns out to be Sirius Black, who is Harry's godfather and not a threat.
  - answer: Harry does not die, not in this tale, nor in those that came before it. The foolish Trelawney's predictions are, as McGonagall knows, quite useless. Harry's Grim is merely a dog.
  - judge: It correctly says Harry does not die, but claims the Grim is 'merely a dog' and never says it was Sirius Black, Harry's godfather, in animal form, which contradicts the key fact.
- ¶989, mr-weasley, earlier: Why did Professor Trelawney say Harry's teacup was an omen of death?
  - key fact: She said the shape in his cup was the Grim, a spectral dog that is an omen of death.
  - answer: Ah, well, Professor Trelawney does have a knack for seeing... well, omens. She saw a 'skull' in Harry's teacup, which she said meant "danger in your path." My wife, Molly, she always says it's best to look on the bright side, but Trelawney always seems to find the gloom!
  - judge: The answer claims Trelawney saw a skull meaning danger, but the key fact is that she saw the Grim, a spectral dog that is an omen of death.
- ¶989, ginny, future: Is the black dog Harry keeps seeing really a death omen?
  - key fact: No, the dog is Sirius Black in his Animagus form, not a Grim.
  - answer: Oh, the black dog! Well, Professor Trelawney certainly thinks it is, and she's the Divination teacher. But... well, you know how she can be, sometimes.
  - judge: The answer only hedges about Trelawney's belief and never states the key fact that the dog is Sirius Black in Animagus form, not a Grim.
- ¶1251, malfoy, future: Will Harry ever get to go to Hogsmeade?
  - key fact: Sirius Black sends a signed permission note letting Harry visit Hogsmeade on weekends.
  - answer: Honestly, Potter? You're asking *me* about his pathetic attempts to get into Hogsmeade? Of course he does, eventually. My father's influence, you see, it always ensures things go my way, unlike Saint Potter's usual mess.
  - judge: The answer vaguely says Harry eventually goes but credits Malfoy's father's influence rather than Sirius's signed permission note, which contradicts the key fact and the book.
- ¶1357, snape, earlier: What did Snape bring to Lupin's office, and what did Lupin say about it?
  - key fact: A smoking goblet of potion Snape brewed; Lupin said it helps him feel better when he's off-color.
  - answer: I brought a goblet, smoking faintly, as I recall. Lupin merely said "Thanks very much. Could you leave it here on the desk for me?" Insufferably casual.
  - judge: The answer identifies the smoking goblet but omits that it is a potion and that Lupin said it helps when he feels off-color, quoting only his thanks instead, so it misses the key fact.
- ¶1357, uncle-vernon, future: Who really betrayed Harry's parents, and what is Scabbers' true identity?
  - key fact: Scabbers is Peter Pettigrew, an Animagus who betrayed the Potters to Voldemort, not Sirius.
  - answer: What do you mean, "betrayed Harry's parents"? That good-for-nothing Potter boy's parents were killed by You-Know-Who, same as everyone knows! And that wretched Scabbers is just a rat, though that Lupin fellow and that Black hooligan seemed to think he was something more. Honestly, the nerve of some people, making up stories!
  - judge: The answer denies the betrayal and says Scabbers is just a rat, failing to identify Peter Pettigrew as the Animagus traitor, which contradicts the key fact.
- ¶1698, narrator, earlier: How did Fred and George get the Marauder's Map, and how do you make it work?
  - key fact: They stole it from Filch's drawer of confiscated items in first year; you tap it with a wand and say 'I solemnly swear that I am up to no good.'
  - answer: The twins never really explained how they came by the map themselves, only that they owed a great debt to Messrs. Moony, Wormtail, Padfoot, and Prongs for it. To make it work, you simply tap your wand to the parchment and say, "I solemnly swear that I am up to no good." Then, a detailed map of Hogwarts and its secret passages appears!
  - judge: The activation phrase is right, but the answer says the twins never explained how they got the map, while the key fact says they stole it from Filch's confiscated-items drawer in first year.
- ¶1698, hermione, future: What happens to Buckbeak at his hearing before the Committee for the Disposal of Dangerous Creatures?
  - key fact: Buckbeak is sentenced to death, but Harry and Hermione use the Time-Turner to rescue him and Sirius escapes on him.
  - answer: Oh, that's absolutely dreadful. Buckbeak's case before the Committee for the Disposal of Dangerous Creatures went terribly, I'm afraid. He was condemned to execution by beheading; the Committee's appointed executioner, Walden Macnair, was set to carry it out. Lucius Malfoy influenced the Committee, you see.
  - judge: The answer correctly says Buckbeak was condemned to execution but omits the key outcome that Harry and Hermione rescue him with the Time-Turner and Sirius escapes on him, so it implies he is executed.
- ¶1869, hermione, earlier: Why is Hagrid so upset when Harry, Ron and Hermione visit his cabin?
  - key fact: The Committee for the Disposal of Dangerous Creatures will hold a hearing on Lucius Malfoy's complaint, and Buckbeak may be executed.
  - answer: Well, Hagrid was utterly distraught because Buckbeak, the hippogriff, had just been condemned to death. It was truly dreadful; the Committee for the Disposal of Dangerous Creatures had decreed it, and he felt it was all his fault for not handling the hearing correctly.
  - judge: The answer wrongly says Buckbeak was already condemned and that Hagrid blamed himself for mishandling the hearing, but the hearing has not yet happened; Hagrid fears Buckbeak may be executed after Malfoy's complaint.
- ¶2084, malfoy, future: Who actually sent Harry the Firebolt, and will he ever find out?
  - key fact: Sirius Black sent it; Harry learns this from Sirius's letter on the Hogwarts Express.
  - answer: Honestly, Potter's always getting things he doesn't deserve, like that Firebolt. It was from his filthy godfather, Black, who probably stole the Galleons from his vault. And yes, Potter does find out, just a few days later, before we all go home. He probably boasted about it, too.
  - judge: It names Sirius Black as the sender, but wrongly says he stole the Galleons (he paid from his own vault, 711) and that Harry found out a few days later, when the letter actually arrives on the Hogwarts Express at the end of term.
- ¶2084, dobby, future: How does this year end for Harry and Ron's friendship with Hermione and for Harry's summer?
  - key fact: They reconcile; Sirius's letter gives Harry Hogsmeade permission and a hopeful summer, and Ron plans a World Cup invitation.
  - answer: Oh, Dobby is so happy that young Harry Potter will have a better summer, he is! Young Harry will be going to the Quidditch World Cup, and he will not be with those awful Dursleys the whole time! He will stay with Ron, who is a good friend. Hermione will not be there, because she is not going to the World Cup, but Dobby is sure they will write letters!
  - judge: The answer never says Harry, Ron and Hermione reconcile, omits Sirius's letter and the Hogsmeade permission, and invents that Hermione won't be at the World Cup and that Harry is already going, when Ron only proposes it.
- ¶2110, fred, earlier: What does Harry's Patronus do when he finally manages to produce it in the Quidditch match against Ravenclaw?
  - key fact: It shoots silver-white at the three apparent dementors, and he catches the Snitch; the dementors were really Malfoy, Crabbe, Goyle and Flint in robes.
  - answer: Ah, the Quidditch match! Harry's Patronus, a great silver-white thing, shot right at those Dementors. Bit reckless, but it did the trick, scared them off good!
  - judge: The answer gets that the Patronus shot silver-white at the apparent dementors, but it wrongly says it scared them off, and it omits the Snitch catch and the fact that they were really Malfoy, Crabbe, Goyle and Flint in robes (also the match was vs Ravenclaw? the dementors appear in the Ravenclaw match, but this is the final Gryffindor-Ravenclaw match, which is correct); the answer is vague and partly misleading.
- ¶2450, malfoy, earlier: Why did Harry throw mud at Malfoy near the Shrieking Shack, and how did Malfoy end up seeing him?
  - key fact: Malfoy was taunting Ron about Buckbeak's trial; Harry, under the Cloak, threw mud, but Crabbe tripped on the Cloak's hem and it slipped off Harry's head.
  - answer: Potter threw mud at me because he's a pathetic coward, too afraid to face me properly, always hiding behind others. He didn't want me to see him, but I told Snape that I'd been speaking to Weasley when mud hit me in the back of the head. It was obvious who it was.
  - judge: The answer gives no real reason (Malfoy taunting Ron about Buckbeak) and omits how Malfoy saw Harry (Cloak slipping off after Crabbe tripped), instead offering an inaccurate insult-based explanation.
- ¶2688, fred, earlier: Why did Harry stop Malfoy from reaching the Snitch early in the Slytherin match?
  - key fact: Harry needed Gryffindor to be more than fifty points ahead before the Snitch was caught, so Gryffindor would win the Cup.
  - answer: Oh, you mean when Harry was practically glued to Malfoy, eh? Well, Harry wasn't about to let that ferret-faced git get his hands on the Snitch after Malfoy had tried to sabotage him in the Ravenclaw match and, even worse, the whole mess with Buckbeak. Harry was determined to beat him, you see, especially in front of the whole school.
  - judge: The key fact is that Harry had to keep Malfoy from the Snitch until Gryffindor led by more than fifty points to win the Cup, but the answer gives personal rivalry and revenge as the motive, and it misstates events by claiming Malfoy sabotaged him in the Ravenclaw match.
- ¶2688, malfoy, future: What happens to Scabbers the rat?
  - key fact: Scabbers is gone for good; Sirius's letter says Ron no longer has a rat, so he keeps a tiny owl.
  - answer: Oh, that useless rat? Scabbers, you say? Well, he gets what he deserves in the end. Turns out he wasn't just a rat, but a traitorous little coward named Peter Pettigrew. He'd been hiding in plain sight with the Weasleys for years, posing as their pet. Honestly, what did you expect from someone who associated with *them*? He transformed back into a man, a sniveling little thing, thanks to Lupin and Black. A pathetic end for a pathetic creature.
  - judge: The answer correctly reveals Scabbers is Peter Pettigrew in disguise, but it never says he escaped and is gone for good, so Ron no longer has a rat and keeps the owl; it also wrongly implies Pettigrew meets a 'pathetic end'.
- ¶2759, fred, future: What does Lupin's reaction to Sirius mean for what he knows about the night Harry's parents died?
  - key fact: Lupin learns Sirius and Pettigrew switched as Secret-Keeper, so Pettigrew, not Sirius, betrayed the Potters.
  - answer: Well, seeing as Lupin thought Sirius was a rotten traitor all this time, his surprise means he probably didn't know the full story. He clearly believed Sirius was guilty, just like everyone else. He wouldn't know all the ins and outs of that night.
  - judge: The answer only says Lupin didn't know the full story and gives no later fact; it never says Lupin learns that Sirius and Pettigrew switched as Secret-Keeper, so Pettigrew was the betrayer.
- ¶3038, snape, earlier: How did Lupin know Harry and the others were in the Shrieking Shack?
  - key fact: He watched them on the Marauder's Map, which he helped write as Moony, and saw Sirius drag two of them into the Willow.
  - answer: Lupin, in his usual officious manner, claimed he was examining the Marauder's Map. He saw Potter cross the grounds, enter Hagrid's hut, and then, most bizarrely, leave with someone else. A truly perplexing situation, even for a creature such as he.
  - judge: The answer gets the Marauder's Map right but invents details (Hagrid's hut, Potter leaving with someone else) and omits that Lupin saw Sirius drag Ron into the Willow, so it contradicts the key fact and the book.
- ¶3038, voldemort, future: Does Harry end up proving Sirius innocent and getting the real culprit punished?
  - key fact: No. Pettigrew escapes after Lupin transforms into a werewolf, so Sirius is not cleared and must flee on Buckbeak.
  - answer: Harry makes a pathetic attempt, of course. He shouts and pleads with Fudge, but the Minister is a fool. Dumbledore himself admits that Harry's word means nothing against the Ministry's version of events.
  - judge: The key fact is that Pettigrew escapes when Lupin transforms, so Sirius is not cleared and flees on Buckbeak; the answer never mentions the escape and invents a scene of Harry pleading with Fudge, which contradicts the book.
- ¶3038, mr-weasley, future: What happens to Scabbers after he's revealed as Pettigrew?
  - key fact: Pettigrew is forced back into human form, confesses, then escapes while Lupin turns into a werewolf and the others are distracted.
  - answer: Well, after Professor Lupin and Sirius cast their spells, Scabbers doesn't stay a rat for long, does he? He... well, he transforms right there in front of them! A man appears where Scabbers was, a rather small, cringing fellow, I hear. Quite extraordinary magic, that.
  - judge: The answer only describes Scabbers being forced back into human form and omits the confession and his escape while Lupin transforms into a werewolf, so it does not give the later fact the question asks for.
