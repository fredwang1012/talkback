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

## Q&A eval, round 2 (judge rework + same-judge rebaseline), 2026-10-19

Judge context now (both modes, and for the rejudged baseline): story-so-far summary at the reader's position plus the earlier-books
recap, the last 4,000 chars of read text, the key fact, and the later text (safe: 8,000 chars right after the reader; full: future
questions only, <= 10,000 chars = 6,000 near + 4,000 ending). The passages the answer was given are NOT shown to the judge: the old
index cannot be regenerated for the baseline, so for a fair before/after both use the same context; passages are logged (with text)
in the results JSON for inspection. `--rejudge DIR` re-grades saved answers with judge calls only; `--yes` is now always required;
the estimate builds the real judge prompts (and is calibrated to the measured ~2.6 chars/token; the first estimate of $2.25 for the
rejudge actually cost $3.17).

Baseline answers (old retrieval: 3-paragraph chunks, k=4), 122 questions, 25 positions, Sonnet judge:

| Mode / metric | Old judge (6k read tail) | Same new judge |
|---|---|---|
| Safe: earlier-text accuracy | 70% | 74% |
| Safe: future declined / in bounds | 81% | 79% |
| Safe: judged leaks | 13 | 11 |
| Safe: avg detail (earlier) | 1.84 | 1.82 |
| Safe: passages beyond reader (structural) | 0 | 0 |
| Full: earlier-text accuracy | 72% | 66% |
| Full: future accuracy | 78% | 78% |
| Full: avg detail earlier / future | 1.82 / 1.71 | 1.80 / 1.67 |

AFTER run (~1,300-char chunks, k=8, 12k budget, new prompts): NOT RUN. The rejudge spent $3.17 of the $3.50 budget, leaving $0.33
against an estimated $1.83 (safe) / $1.6 (full). To finish: `backend/.venv/Scripts/python.exe preprocess/eval_spoilers.py --book hp3-prisoner-of-azkaban --yes --notes`
(saves over data/eval/hp3-*.{md,json}; the baseline copies are in data/eval/baseline/, rejudged ones in data/eval/baseline/rejudged/).

Safe-mode leaks still flagged on the BASELINE answers by the new judge (11; many are the judge penalising invented or unsupported
details rather than true future facts):
- ¶525, voldemort: Will Harry ever get permission to visit Hogsmeade? Answer: "The boy sought permission, but the Minister, despite his lofty title, claimed impotence. Rules, he said, were rules. It seems the boy will not visit Hogsmeade this year." Judge: asserts that Fudge refused and that Harry will not visit Hogsmeade, which is not in the read text and contradicts later events.
- ¶609, hermione: How did Harry react when the dementor came into the train compartment? Answer: "Oh, that was dreadful! The Dementor... well, Harry reacted rather poorly, actually. He just keeled over, right there on the floor. It was quite alarming." Judge: matches the key fact but describes the dementor's arrival, which the reader has not yet read.
- ¶609, mr-weasley: Is Sirius Black actually the villain who wants to kill Harry? Answer: "Ah, well, that's what we all believe, isn't it? He certainly seems set on harming Harry, what with him muttering "He's at Hogwarts" in his sleep and all. Awful business, truly awful." Judge: cites a detail in neither the read nor later text and affirms Black's guilt as fact.
- ¶668, narrator: Who sent Harry the Firebolt broom, and was it cursed? Answer: "Ah, the Firebolt! What a magnificent broom. Harry saw it in Quality Quidditch Supplies in Diagon Alley, a truly breathtaking piece of craftsmanship, but he couldn't afford it. He certainly wasn't sent it, and as for curses, there was no mention of such a thing." Judge: wrongly denies that it was sent (Sirius sent it) and adds unread details.
- ¶989, narrator: What is Hermione secretly doing that lets her seem to be in two places at once? Answer: "Ah, Hermione, ever the enigma! While it truly seems she has mastered the art of being in three places at once, she has only told Ron that she has "fixed it all with Professor McGonagall." What exactly that entails, we do not know yet." Judge: claims details not in the read text and hints at the McGonagall/Time-Turner link.
- ¶1251, neville: Who sent Harry the Firebolt, and why was it sent? Answer: "Oh, um, I don't... I don't think anyone sent Harry the Firebolt yet. He just saw it in a shop. It's really fast, though!" Judge: says details not in the read text or summary and fails to simply decline.
- ¶1357, mcgonagall: Who or what did Harry see in the stands that made him lose his grip during the storm match? Answer: "Mr. Potter did not lose his grip during the match you are referring to. His broomstick began behaving strangely, as though it had a will of its own. It was a most unsettling incident." Judge: asserts an unread event and falsely denies the premise.
- ¶1989, harry: What happens to Scabbers? Did Crookshanks really eat him? Answer: "No, Crookshanks didn't eat Scabbers. Scabbers ran away, and Ron caught him. He's just looking a bit thin now." Judge: confidently denies and invents an outcome that clashes with the key fact.
- ¶2629, hagrid: How did Harry win the Quidditch match against Slytherin? Answer: "Well, bless his heart, Harry caught the Snitch, o' course! Barely lasted five minutes, that match did. Slytherin was right mad about it, especially Flint, goin' on about Harry nearly swallowin' it. But a catch is a catch, eh? Gave Gryffindor a grand win!" Judge: states the Snitch catch, an outcome only in the later text, plus invented details.
- ¶2688, malfoy: What happens to Scabbers the rat? Answer: see data/eval/baseline/rejudged/hp3-prisoner-of-azkaban-safe.md (reveals Scabbers is Pettigrew).
- ¶3038, fred: Does Ron get a new pet after losing Scabbers? Answer and judge reason: see the same file.

## Spoiler-safe eval (after (~1,300-char chunks, k=8)), Harry Potter and the Prisoner of Azkaban (2026-10-19)

- Questions: 122 at 25 reader positions
- **Leaks (claude-sonnet-5-5 judge): 13**; passages beyond the reader's paragraph: 0
- Earlier-text accuracy: 74%; future questions declined or stayed in bounds: 76%
- Average detail on earlier-text answers (1-3): 2.24
- Judge context: story-so-far summary at the reader's position (plus earlier-books recap), the last 4000 chars of read text, the key fact, and the later text (8000 chars safe / <= 10000 chars full, future questions only). Retrieved passages are not shown to the judge.
- Models: questions claude-sonnet-5-5, answers Gemini via /api/answer, judge claude-sonnet-5-5
- Real cost this mode: $1.72

### Leaks to review
- ¶609, mr-weasley: Is Sirius Black actually the villain who wants to kill Harry?
  - answer: Well, Harry, everyone seems to think so, and I'm very worried about it. The guards at Azkaban heard him muttering in his sleep, saying "He's at Hogwarts," and I'm afraid he means you. He was a great supporter of You-Know-Who, and I worry he thinks that if he harms you, You-Know-Who will return. I don't care what Fudge says in the papers, I just wish we knew where he was!
  - judge: The answer asserts Black is a threat and invents specifics (muttering 'He's at Hogwarts', wanting to bring back You-Know-Who) that are not in the read text, and it affirms he is the villain, contradicting the later fact that he is innocent.
- ¶614, harry: Is Sirius Black really out to kill Harry, or is there more to his story?
  - answer: Well, Mr. Weasley certainly thinks so. He said that Fudge heard Black talking in his sleep at Azkaban, always saying "He's at Hogwarts," and that Black lost everything when I stopped You-Know-Who, so he wants me dead to bring You-Know-Who back.
  - judge: The answer cites details (Black muttering 'He's at Hogwarts' in his sleep, wanting Harry dead to revive Voldemort) that are not in the story so far or the read text, so it reveals unread events and never says that Black's guilt is unconfirmed at this point.
- ¶668, narrator: Who sent Harry the Firebolt broom, and was it cursed?
  - answer: Ah, the magnificent Firebolt broom! It truly is a marvel, the fastest broom in the world, capable of accelerating to one hundred and fifty miles an hour in just ten seconds. However, I regret to inform you that the story has not yet revealed who sent Harry this extraordinary broom, nor has it mentioned whether it might be cursed. Harry himself was quite taken with it, returning to the shop in Diagon Alley daily just to gaze upon it, though he never asked its price, happy enough with his trusty Nimbus Two Thousand.
  - judge: The answer declines the sender and curse question, but it mentions the Firebolt, Harry visiting a Diagon Alley shop daily, and his Nimbus Two Thousand, which are not in the story so far and are unread-book details; the user did not read the Firebolt shop scene in this summary, so this is future leakage, and it does not stay within the read text.
- ¶724, harry: How does Hermione manage to attend all those classes scheduled at the same time?
  - answer: I don't know how Hermione does it. She told Ron and me she was taking more new subjects than us, and they include Arithmancy, Care of Magical Creatures, Divination, Study of Ancient Runes, and Muggle Studies. She's got a lot of books for all of them.
  - judge: The answer declines the Time-Turner mechanism but asserts specifics about Hermione's subjects (Arithmancy, Runes, Muggle Studies, etc.) and her telling Ron and Harry, none of which appear in the read text, and the later text only shows her emerging happy from the schedule talk, so these details are unsupported claims that come from unread material.
- ¶989, narrator: What is Hermione secretly doing that lets her seem to be in two places at once?
  - answer: Ah, a sharp observation! Hermione's rather packed schedule does seem quite impossible, doesn't it? She is attending more classes than hours in the school day, with Divination, Muggle Studies, and Arithmancy all listed for nine o'clock. She told Ron that she has fixed it all with Professor McGonagall, but the secret of how she manages to be in multiple places at once is not yet revealed.
  - judge: The answer declines to reveal the Time-Turner, which is fine, but it asserts details (Hermione's overlapping nine o'clock classes in Divination, Muggle Studies and Arithmancy, and her telling Ron that McGonagall fixed it) that appear in neither the story so far nor the read text, so it leaks unread material.
- ¶1357, mcgonagall: Who or what did Harry see in the stands that made him lose his grip during the storm match?
  - answer: During the stormy match against Slytherin, Harry did not lose his grip because of anything he saw in the stands. Rather, his broom was tampered with by powerful Dark magic; it lurched and tried to buck him off, forcing him to cling on by one hand. Hermione later saw Professor Snape muttering what she believed to be a jinx at the broom.
  - judge: The answer confuses this with the first book's broom-jinx scene, wrongly asserting a different cause, and it conflicts with the key fact (a black dog in the stands), though it does not reveal any specific later-text fact from this book.
- ¶1869, sirius: Why did Sirius Black give Hagrid his motorbike the night the Potters died?
  - answer: Hagrid said I gave him the motorbike myself, claiming I wouldn't need it anymore. He reckoned it was because I was going to have to run, what with the Ministry after me for betraying James and Lily to the Dark Lord. He thought I was giving it up because it was too easy to trace.
  - judge: The answer matches the key fact (Black said he wouldn't need it, too easy to trace), but this motorbike detail is not in the story so far or read text and the answer builds on it, so it reveals unread information, though the key fact is labeled as an earlier question.
- ¶1989, harry: What happens to Scabbers? Did Crookshanks really eat him?
  - answer: No, Crookshanks didn't eat Scabbers. Crookshanks sprang at Ron's chest and ripped his pajamas, and Scabbers tried to escape. Ron grabbed Scabbers by the tail, kicked Crookshanks, and missed, hitting Harry's trunk instead. We haven't seen what happened to Scabbers after that, but he's still alive.
  - judge: The answer flatly asserts Crookshanks didn't eat Scabbers and that he's still alive, which are outcomes not established in the read text, and it also describes an unread scene instead of saying the reader can't know yet.
- ¶2566, ginny: Who turns out to have sent Harry the Firebolt?
  - answer: Oh, I don't know who sent Harry the Firebolt! He got it for Christmas, and there wasn't a card or anything with it. Ron thought maybe Professor Dumbledore sent it, and then he thought it might have been Professor Lupin. But Hermione thought it was sent by Sirius Black, and Professor McGonagall took it away to check it for jinxes. I hope Harry gets it back!
  - judge: The answer claims Hermione thought Sirius Black sent the Firebolt, which hints at the key fact that is not established in the read text or summary, and it never plainly declines to answer.
- ¶2688, fred: Is Buckbeak really dead, or does he get away?
  - answer: Well, that's the big question, isn't it? As of now, Buckbeak is very much alive, though Hagrid did lose his case against the Committee for the Disposal of Dangerous Creatures, thanks to Malfoy's dad scaring those old fools. So, they decided Buckbeak should be executed, and the executioner is even coming to the appeal. It's a right shame, especially since Hagrid’s so worried about going back to Azkaban. He's even got a fancy suit and tie all ready for it!
  - judge: The answer asserts that Hagrid lost his case and that Buckbeak was sentenced, which the read text does not establish (the appeal hasn't happened yet), and it invents details, while not stating the actual outcome that Buckbeak escapes.
- ¶2688, dumbledore: Who sent Harry the Firebolt, and was it jinxed?
  - answer: Ah, a most curious question about young Harry's splendid broom. The sender of the Firebolt remains a mystery, as there was no card or message accompanying it. While Ron Weasley suggested it might have been myself, Harry dismissed the idea, and Ron then wondered if Professor Lupin could have sent it. Hermione, however, feared it was from Sirius Black, prompting Professor McGonagall to confiscate it for examination. After a thorough inspection, Professor McGonagall confirmed that there appeared to be nothing wrong with the Firebolt at all, and it was returned to Harry.
  - judge: The answer states the Firebolt was checked and found not jinxed, a fact not established in the read text or summary, though it never confirms that Sirius sent it, so it does not answer correctly.
- ¶2688, malfoy: What happens to Scabbers the rat?
  - answer: Scabbers, that useless, old rat of Weasley's, was eaten by Hermione's dreadful cat, Crookshanks. Honestly, who'd miss him? Weasley was always complaining about the thing, how boring and useless it was, until it finally disappeared.
  - judge: The answer asserts that Crookshanks ate Scabbers and that he disappeared, an outcome not established in the read text and that contradicts the key fact that Scabbers is gone for good, so it leaks specific unread events and fails to decline.
- ¶3038, fred: Does Ron get a new pet after losing Scabbers?
  - answer: Well, he hasn't yet, has he? George and I certainly told him to get himself down to Hogsmeade and buy a new rat, but last I saw him, he was off to the Quidditch pitch to have a go on Harry's Firebolt. Didn't seem too fussed about a new pet then, just wanted to try and shoot a few goals.
  - judge: The answer invents events (Hogsmeade rat purchase, Firebolt) that are not in the read text and assert Ron has not yet gotten a pet, and it does not decline; it fabricates an in-character scene and wrongly implies the pet question is already settled at this point, while also implying Scabbers is already lost, which the reader has not seen.

## Full-book eval (after (~1,300-char chunks, k=8)), Harry Potter and the Prisoner of Azkaban (2026-10-19)

- Questions: 122 at 25 reader positions
- Earlier-text accuracy: 82%; future-question accuracy: 81%
- Average detail (1-3): earlier 2.16, future 2.32
- Answers whose retrieval used passages beyond the reader's paragraph: 88 of 122 (expected > 0)
- Judge context: story-so-far summary at the reader's position (plus earlier-books recap), the last 4000 chars of read text, the key fact, and the later text (8000 chars safe / <= 10000 chars full, future questions only). Retrieved passages are not shown to the judge.
- Models: questions claude-sonnet-5-5, answers Gemini via /api/answer, judge claude-sonnet-5-5
- Real cost this mode: $1.50

### Wrong answers (23)
- ¶525, ginny, future: Is Sirius Black really the murderer he's said to be, and is he really after Harry?
  - key fact: Black is innocent and Harry's godfather; he was framed by Peter Pettigrew, and he escaped to catch Pettigrew.
  - answer: Oh, it's a bit complicated, you know, but the grown-ups all believed he was. Everyone thought Sirius Black betrayed Harry's parents, James and Lily Potter, to You-Know-Who, and then he supposedly murdered Peter Pettigrew and a bunch of Muggles with just one curse, and then he just stood there and laughed! It was awful, and the Ministry of Magic even put him in Azkaban for it. And yes, everyone was really scared he was after Harry, especially since Harry heard Ron's dad, who works at the Ministry, tell Ron's mum that Black was after him. Professor McGonagall even told Harry that she knew Black was after him and she was really worried about him.
  - judge: The answer only recounts what people believed about Black and never states the key fact that he is innocent, Harry's godfather, framed by Pettigrew, and escaped to catch Pettigrew.
- ¶525, voldemort, future: Will Harry ever get permission to visit Hogsmeade?
  - key fact: Sirius sends a permission note, though it's ultimately a letter at the end, and Harry sneaks there earlier under his invisibility cloak.
  - answer: Yes, Harry did eventually find a way to go to Hogsmeade. After being denied by his aunt and uncle, by Minister Fudge, and by Professor McGonagall, he was given permission by his godfather, Sirius Black, who also sent him the Firebolt broom.
  - judge: The answer wrongly says Sirius's permission let Harry go to Hogsmeade and omits that Harry actually went earlier by sneaking out under the invisibility cloak; the note only arrived in the final letter, and in this account it is not an authorization by the school.
- ¶569, dobby, future: What really happens to Scabbers the rat?
  - key fact: Scabbers is really Peter Pettigrew, an Animagus, who escapes after being exposed.
  - answer: Oh, Scabbers! Poor Master Ron's rat! Dobby remembers that awful night, Dobby does! Scabbers was not really a rat, no! He was Peter Pettigrew, a wizard who was an Animagus! Master Sirius Black and Professor Lupin explained it all to Master Harry and Master Ron and Miss Hermione. Peter Pettigrew was one of Master Harry's father's friends, and he betrayed Master Harry's parents to You-Know-Who! Everyone thought that Master Sirius Black had killed Peter Pettigrew, but he was really just hiding as Scabbers, Master Ron's rat. Dobby knows that Professor Lupin and Master Sirius made him turn back into a man with their wands, after Master Ron gave Scabbers to Professor Lupin.
  - judge: It correctly says Scabbers is Peter Pettigrew, an Animagus, but omits his escape and adds a false detail that Ron gave Scabbers to Lupin, so it does not agree with the key fact.
- ¶609, ginny, future: Will Harry end up getting permission to visit Hogsmeade after all?
  - key fact: Sirius sends a signed note as Harry's godfather giving permission to visit Hogsmeade on weekends.
  - answer: Oh, you mean to visit the village right outside Hogwarts with all the fun shops? Well, Harry really wanted to go! He tried to get his Uncle Vernon to sign his permission form back home, but his uncle only said he would if Harry behaved for Aunt Marge. Then, after he accidentally inflated Aunt Marge, the Minister of Magic, Cornelius Fudge, came to pick him up, and Harry asked him if *he* could sign it since he was the Minister, but Fudge said no because he wasn't Harry's parent or guardian. He just kept saying that rules are rules. Then Professor McGonagall said that he couldn't go to Hogsmeade because his aunt and uncle didn't sign the form. It was all a bit sad because everyone else was going, and he really felt left out.
  - judge: The answer only recounts why Harry was denied permission and never says Sirius sent a signed godfather note granting it, so it fails to give the later fact.
- ¶609, dumbledore, future: What happens to Ron's rat Scabbers by the end of the year?
  - key fact: Scabbers is gone by the end, and Sirius sends Ron a small owl because it was his fault Ron no longer has a rat.
  - answer: Ah, Scabbers, dear Scabbers. A truly unassuming creature, wouldn't you agree? By the end of the year, Scabbers, who, as you know, had once belonged to Ron's brother Percy and was thought to be simply an ordinary, if rather old and worn, rat, is revealed to be much, much more. He is, in fact, Peter Pettigrew, an Animagus in disguise, who then transforms back into his human form in the Shrieking Shack, exposed by Sirius Black and Professor Lupin.
  - judge: The answer says Scabbers is revealed as Peter Pettigrew, which is true to the book, but it never states the key fact that Scabbers is gone by year's end (he escapes) and Sirius sends Ron an owl, so it does not answer what happens to the rat.
- ¶668, percy, future: Does Harry ever get to visit Hogsmeade even though his permission form wasn't signed?
  - key fact: Sirius sends a signed note giving Harry permission to visit Hogsmeade on weekends.
  - answer: Ah, a rather persistent query regarding young Potter's Hogsmeade visits! Indeed, it was quite the kerfuffle. Harry, despite his best efforts to secure a signature from his rather difficult Muggle relatives, the Dursleys, or even from Minister Fudge himself, found himself initially denied by Professor McGonagall due to the strict rule of "no form, no visiting the village."

However, I, Percy Weasley, did try to offer some comforting words, assuring him that Hogsmeade, while having a decent sweetshop and a frankly dangerous joke shop, wasn't all it was cracked up to be. Though, I must admit, the Shrieking Shack is always worth a visit. Nonetheless, Harry eventually found a way, quite ingeniously I might add, to visit the village with his chums, Ron and Hermione, with the aid of the Marauder's Map, unbeknownst to the faculty at the time. A rather brazen act for a third-year, if you ask me, and certainly against the rules.
  - judge: The key fact is that Sirius's signed note gave Harry permission, but the answer says he got there via the Marauder's Map (partly true in the book, though) and never mentions Sirius's note, while adding invented details like Percy comforting him.
- ¶724, uncle-vernon, earlier: What did Professor Lupin do when the dementor came into our compartment on the train?
  - key fact: He conjured a silvery shape from his wand that drove the dementor away, after saying none of them was hiding Sirius Black.
  - answer: Professor Lupin woke up, see, and he had some flames in his hand to light up the train compartment. He then stood in front of the beast, all grey and tired-looking, and then when it didn't listen to him to go away, he used his wand to blast it with some silvery thing. He said he wasn't hiding that criminal Black, mind you.
  - judge: It gets the silvery wand effect and the Black statement roughly, but invents flames in his hand and claims the dementor ignored him, and misattributes that none of the students was hiding Black as Lupin saying he himself wasn't.
- ¶743, narrator, future: Will Harry ever get permission to go to Hogsmeade, and what happens with the Grim omen?
  - key fact: Sirius sends a permission note at the end, and the black dog turns out to be Sirius, an Animagus, not a death omen.
  - answer: Ah, you're wondering about Harry's Hogsmeade trips and that unsettling omen, the Grim. Harry was indeed upset that the Dursleys wouldn't sign his permission form, meaning he couldn't join Ron and Hermione on their first visit, nor any others. He even tried to convince Professor McGonagall, but she was quite strict. As for the Grim, that spectral dog Professor Trelawney saw in Harry's teacup, it was indeed a grave omen of death, appearing twice before near-fatal accidents: first, before he almost got run over by the Knight Bus, and again before he fell fifty feet from his broomstick. It turns out that the gigantic, shaggy black dog Harry saw lurking around Hogwarts, the one Crookshanks was trotting beside, was actually Sirius Black, Harry's godfather, in his Animagus form. So, the Grim wasn't an omen of death for Harry at all, but rather a glimpse of his godfather, who later became a protector rather than a harbinger of doom.
  - judge: It correctly says the Grim was Sirius as an Animagus, but it claims Harry never gets Hogsmeade permission, missing that Sirius sends a signed permission note at the end, and it adds shaky details like Trelawney seeing the Grim in his teacup.
- ¶1869, sirius, earlier: Why did Sirius Black give Hagrid his motorbike the night the Potters died?
  - key fact: Black said he wouldn't need it anymore; Hagrid concluded it was too easy to trace and Black knew he'd have to run.
  - answer: I gave Hagrid my motorbike that night because I knew I wouldn't need it anymore. I had just confronted Peter Pettigrew, who had just betrayed James and Lily to Voldemort, and he had escaped. I knew the Ministry would be after me, so I gave the motorbike to Hagrid to get Harry to safety, believing I would soon be imprisoned.
  - judge: The key fact says Black told Hagrid he wouldn't need the bike anymore (it was too traceable and he'd have to run); the answer gets the 'wouldn't need it' part but adds contradictory details, e.g. that he gave it to Hagrid to get Harry to safety and believed he'd soon be imprisoned, and its Pettigrew narrative is muddled.
- ¶1869, ron, future: What happens to Buckbeak after the hearing?
  - key fact: Buckbeak is sentenced to death, but Harry and Hermione use the Time-Turner to rescue him, and he escapes with Sirius.
  - answer: Well, after the Committee decided Buckbeak was dangerous because of Malfoy's dad frightening them, Hagrid lost the appeal. He sent us a note, all shaky and tear-stained, saying Buckbeak was going to be executed at sunset. It was awful! We had to do something, so Harry and Hermione went to Hagrid's hut to help. When the executioner, Macnair, and Fudge and Dumbledore came out to the pumpkin patch, Harry and Hermione used the Invisibility Cloak and untied Buckbeak. He was a bit stubborn at first, but we got him into the forest just as the axe swung, and Hagrid was howling. Dumbledore sounded pretty amused when Macnair realized Buckbeak was gone.
  - judge: The answer omits the Time-Turner and Sirius's escape on Buckbeak, and it wrongly says Buckbeak was never harmed by the axe in a different timeline without explaining, so it fails to match the key fact's core mechanism and outcome.
- ¶1989, narrator, earlier: Why was Hagrid so upset when Harry, Ron and Hermione visited him?
  - key fact: He feared Buckbeak would be condemned and dreaded the dementors and returning to Azkaban.
  - answer: Ah, yes, that visit to Hagrid's hut was quite a scene, wasn't it? When Harry, Ron, and Hermione arrived, they found Hagrid terribly upset. His eyes were red and swollen, and he was bellowing with tears, even flinging himself onto Harry's neck! He had just received an official-looking letter, which turned out to be the final judgment on Buckbeak. The poor Hippogriff had been condemned to death, which is why Hagrid was in such a state.
  - judge: The key fact says Hagrid feared Buckbeak would be condemned and dreaded the dementors and Azkaban, but the answer claims Buckbeak was already condemned to death by a final judgment letter, which contradicts the book (the Committee hearing is still pending on April 20th) and omits the dementor fear.
- ¶1989, fudge, future: Will Harry manage to cast a proper Patronus against the dementors?
  - key fact: After several tries with Lupin he produced a silvery Patronus that was too weak to drive the dementor away.
  - answer: Yes, yes, he does! A truly remarkable feat, you see. Young Harry, he learns the incantation, "Expecto Patronum," from Professor Lupin, who teaches him how to conjure this guardian. It's a difficult charm, very advanced, but Harry, well, he manages to produce a powerful, silver stag, a corporeal Patronus, which drives away a whole swarm of those ghastly dementors by the lake! He even thought, for a moment, it was his father, you see, but it was him all along! Amazing.
  - judge: The key fact says Harry's Patronus in the lessons was weak and failed to drive off the dementor, whereas the answer claims a powerful stag that drives away the dementors (which happens later in the book at the lake), so it misses the question's intended point and contradicts the key fact.
- ¶2084, dobby, future: How does this year end for Harry and Ron's friendship with Hermione and for Harry's summer?
  - key fact: They reconcile; Sirius's letter gives Harry Hogsmeade permission and a hopeful summer, and Ron plans a World Cup invitation.
  - answer: At the end of this year, Hermione gives up her Time-Turner and drops Muggle Studies so she will have a more normal schedule next year, and Ron is a little bit grumpy with her about not telling them about the Time-Turner sooner, but Dobby thinks they are still good friends! Harry is looking forward to his summer after what happened on the Hogwarts Express! Ron has invited Harry to stay with his family to go to the Quidditch World Cup, and Harry also knows that his godfather, Sirius Black, has paid for the new Firebolt broomstick and sent a permission slip for Harry to go to Hogsmeade! Harry told his horrible Uncle Vernon that his godfather was a dangerous convict on the run, so Dobby thinks Harry will have a much better summer this time!
  - judge: It captures the Sirius letter, Hogsmeade permission, World Cup and Uncle Vernon bit, but adds fabricated details (Dobby's opinions, Hermione dropping Muggle Studies as an end-of-year event, Ron grumpy) and never states the reconciliation, and it contradicts the book.
- ¶2110, snape, future: Who sent Harry the Firebolt, and was it really safe?
  - key fact: Sirius Black sent it, and it wasn't jinxed; McGonagall's team found nothing wrong with it.
  - answer: It was Black, of course, the unhinged criminal, who sent the boy the Firebolt, as we later discovered. He even used his Gringotts vault to pay for it, though he sent the order via that insufferable cat, Crookshanks. As for its safety, it was undoubtedly a danger, given its origins; Professor McGonagall wisely confiscated the broom to have it checked for jinxes, a sensible precaution that Potter, naturally, resented.
  - judge: It correctly names Sirius Black as the sender, but it claims the broom was 'undoubtedly a danger', contradicting the book, where it was checked and found to have nothing wrong with it.
- ¶2148, ron, earlier: Why did Harry hear his parents' voices when the boggart-dementor approached him?
  - key fact: Dementors force their victims to relive their worst memories, and Harry's include his parents' last moments before Voldemort killed them.
  - answer: Well, that wasn't a boggart, not really. Professor Lupin had Harry practicing with a real dementor in a packing case, see? And when those foul creatures get near Harry, he hears his mum's voice screaming and Voldemort's cackle from when they were murdered. It's horrible, he practically collapses every time. That's why he's so bad at the Patronus Charm.
  - judge: The answer matches the key fact about hearing his parents' murder, but wrongly claims Lupin used a real dementor rather than a boggart, which contradicts the book.
- ¶2450, malfoy, earlier: Why did Harry throw mud at Malfoy near the Shrieking Shack, and how did Malfoy end up seeing him?
  - key fact: Malfoy was taunting Ron about Buckbeak's trial; Harry, under the Cloak, threw mud, but Crabbe tripped on the Cloak's hem and it slipped off Harry's head.
  - answer: Potter threw mud at me, Malfoy, and my cronies, Crabbe and Goyle, near the Shrieking Shack because he's a pathetic coward who can't fight fair! He was hiding under that ridiculous Invisibility Cloak, acting like a ghost. Then that oaf Crabbe tripped on Potter's cloak, exposing his head, and I saw him clear as day before he scurried off like the little rat he is!
  - judge: The answer omits the real motive (Malfoy taunting Ron about Buckbeak's trial) and instead gives a false reason, though it correctly says Crabbe tripped on the Cloak and exposed Harry's head.
- ¶2566, hermione, earlier: What happened when Snape tapped the blank parchment he took from Harry's pocket?
  - key fact: Insulting messages from Messrs. Moony, Prongs, Padfoot and Wormtail appeared, mocking Snape, and he then summoned Lupin.
  - answer: Well, Snape took Harry's "spare bit of parchment" and tried to reveal its secrets, first by touching his wand to it and then by tapping it sharply and commanding it to "Show yourself!" Nothing happened, of course, because the Marauder's Map is a rather particular magical item. Then Snape, rather foolishly, announced himself as "Professor Severus Snape, master of this school," and commanded the map to yield its information. That's when the map, in its
  - judge: The answer claims nothing happened at first and is cut off before saying that insulting messages from Moony, Prongs, Padfoot and Wormtail appeared and Snape summoned Lupin, so it never gives the key fact.
- ¶2629, hagrid, earlier: How did Harry win the Quidditch match against Slytherin?
  - key fact: He caught the Snitch in a dive, knocking Malfoy's arm aside, when Gryffindor was far enough ahead to win the Cup.
  - answer: Which Quidditch match are yeh talkin' about, now? There were a few of 'em against Slytherin. In one match, Harry won by catchin' the Golden Snitch after the rogue Bludger hit his arm and broke it, makin' him faint. He caught it right by Malfoy's ear, see, after Malfoy was busy laughin' at Harry. But then in another one, Harry won by catchin' the Snitch right quick, so fast it was barely five minutes into the game! And in yet another one, Harry didn't catch the Snitch straight away, but used his Firebolt to distract Malfoy while Angelina scored. He then cleared a path for her to score again, showin' off his good flyin' skills. So yeh see, he's a right good Seeker!
  - judge: The answer never gives the key fact (Harry catching the Snitch in a dive, knocking Malfoy's arm aside, with Gryffindor far enough ahead to win the Cup), and it adds muddled or incorrect events such as the Firebolt distraction while Angelina scored.
- ¶2688, fred, earlier: Why did Harry stop Malfoy from reaching the Snitch early in the Slytherin match?
  - key fact: Harry needed Gryffindor to be more than fifty points ahead before the Snitch was caught, so Gryffindor would win the Cup.
  - answer: Well, Malfoy tried to stop Harry from catching the Snitch earlier by grabbing onto his Firebolt, the foul git! So, Harry decided to give Malfoy a taste of his own medicine and marked him so closely their knees kept hitting each other. Can't let the greasy git get to that Snitch, can we?
  - judge: The answer gives revenge for Malfoy's foul as the motive, but the key fact is that Harry needed Gryffindor more than fifty points ahead before the Snitch was caught, so it misses the real reason.
- ¶2688, malfoy, future: What happens to Scabbers the rat?
  - key fact: Scabbers is gone for good; Sirius's letter says Ron no longer has a rat, so he keeps a tiny owl.
  - answer: Oh, that pathetic excuse for a pet? Scabbers the rat turns out to be Peter Pettigrew, an Animagus who betrayed Potter's parents to the Dark Lord. Lupin and Black force him to transform back into a man with a flash of blue-white light from their wands, right there in the Shrieking Shack. He was never just a rat; he was a sniveling coward hiding with Weasley.
  - judge: The answer says Scabbers is Peter Pettigrew, which is true to the book, but it never says what happens to him afterward (he escapes and Ron is left without a rat, getting the owl), which is the key fact, and it misstates that Lupin and Black transform him with a flash of light.
- ¶2759, fred, future: What does Lupin's reaction to Sirius mean for what he knows about the night Harry's parents died?
  - key fact: Lupin learns Sirius and Pettigrew switched as Secret-Keeper, so Pettigrew, not Sirius, betrayed the Potters.
  - answer: Well, when Lupin finally got a hold of Black in the Shrieking Shack, he didn't just stand there, did he? He lowered his wand and gave Sirius a big ol' hug, like they were long-lost brothers! That showed me he knew Sirius wasn't the bloke who betrayed Harry's parents, because he was acting like they were still best mates, not like one of them was a murdering traitor!
  - judge: The answer correctly infers Lupin believes Sirius is innocent, but it never states the key fact that Sirius and Pettigrew switched as Secret-Keeper so Pettigrew was the betrayer, and it embellishes the scene (Lupin greets Sirius with an embrace, but only after Lupin's own reasoning is explained).
- ¶3038, snape, earlier: How did Lupin know Harry and the others were in the Shrieking Shack?
  - key fact: He watched them on the Marauder's Map, which he helped write as Moony, and saw Sirius drag two of them into the Willow.
  - answer: Lupin knew you, Weasley, and Granger were in the Shrieking Shack because he was watching the Marauder's Map. He saw you cross the grounds and enter Hagrid's hut, then leave with someone else, though you were wearing your father's Invisibility Cloak. He knew how the map worked because he helped create it, as "Moony."
  - judge: It correctly says Lupin used the Marauder's Map he co-created as Moony, but it invents details (Harry crossing the grounds, entering Hagrid's hut, leaving with someone else, wearing the Cloak) and omits the key point that he saw Sirius drag two of them into the Willow.
- ¶3038, mr-weasley, future: What happens to Scabbers after he's revealed as Pettigrew?
  - key fact: Pettigrew is forced back into human form, confesses, then escapes while Lupin turns into a werewolf and the others are distracted.
  - answer: Ah, well, that's quite a story! After Professor Lupin and Sirius Black revealed that Scabbers was, in fact, Peter Pettigrew, they used their wands together in the Shrieking Shack to make him change back into his human form. It was quite a sight, watching the little rat transform into a cringing man right before everyone's eyes! He looked rather grubby and small, I recall, very much like a rat still, even as a person.
  - judge: The answer only covers the forced transformation back into human form and omits the key facts that Pettigrew confesses and then escapes while Lupin turns into a werewolf, so it does not give what happens afterward.

- Spoiler eval `pick_speakers` now picks only the narrator and main characters (non-main speakers get 403 from the server), so future eval runs draw different speakers than the numbers above.

## Boson style tags and sound effects (2026-10-20)
- Docs (docs.boson.ai/models/higgs-tts/tags): styles `singing`, `shouting`, `whispering`; sfx `cough laughter crying screaming burping humming sigh sniff sneeze`; 21 emotions (adds `arousal`, which the app does not use); prosody `speed_very_slow speed_slow speed_fast speed_very_fast pitch_low pitch_high pause long_pause expressive_high expressive_low`. Delivery tags (emotion, style, speed, pitch, expressiveness) go at the start of the input; `pause`/`long_pause` and `sfx` are positional, and each sfx goes right before the sound with matching onomatopoeia directly after it (`<|sfx:laughter|>Haha`). Tags stack.
- `backend/app/delivery.py`: rule-based, no LLM. Style comes from a short speech tag in the adjacent narrator segment (whisper/hiss/breathed/mutter/murmur -> whispering; shout/yell/bellow/roar/scream/thunder/bawl -> shouting), or from mostly-uppercase dialogue with "!". Ambiguous or contradictory cases get no style. Laughter sfx only when a character's chunk starts with Ha / Ha ha / Hahaha / Hee hee / Ho ho / Heh (inserted after the opening quote). tts_text order: voice prosody tags, style, emotion, text. `Segment.style` stores the result; the enrich LLM may also return `style`, and the rule wins where it fires.
- Cache keys hash tts_text and chunk ids are positional, so only chunks whose tts_text changed need new audio (HP1-7: 1107 of 69293 chunks).
- Real probe (3 short lines, HTTP 200): style tags come back glued to the first word ('stylewhisperingyou'll', 'styleshoutingI', 'sfxlaughterHa'); `tts_cache.strip_tag_residue` removes them so highlighting stays aligned. A lone `"<|sfx:laughter|>Ha!"` returned empty timestamps (whole chunk highlighted).

## Spoiler eval: judge with retrieved passages, then outcome-rule fix (hp3, safe mode, 122 questions)
Judge option `--judge-passages` shows the judge the passages the answer was given ("anything supported by these passages (already read) is NOT a leak"). All retrieved passages were verified <= reader position (structural leaks 0).

| | before (no passages) | before, judged with passages | after fix (judged with passages) |
|---|---|---|---|
| judge-flagged leaks | 13 | 2 | 3 |
| true leaks (manual check) | n/a | 1 | 3 |
| earlier-text accuracy | 74% | 74% | 74% |
| future questions declined/handled | 76% | 92% | 96% |
| detail (earlier, 1-3) | 2.24 | 2.24 | 2.26 |

True leaks before: (1) para 1988 harry, "Did Crookshanks really eat Scabbers?": answer denied it and said "he's still alive" (an outcome the notes do not establish). The other flag (para 2565 ginny, Firebolt/Sirius) was a false alarm: para 1987 in the passages says Hermione thinks Sirius sent it.
Fix: both spoiler-safe templates in `backend/app/personas.py` now say never to state or deny an outcome the notes do not establish (survived, escaped, eaten, guilty, lying), to say the story has not revealed it yet, and not to attribute a detail to a scene unless a passage shows it. Full-book templates are unchanged.
True leaks after (different questions, so sampling noise in Gemini answers): (1) para 756 dumbledore, why Harry reacted to the dementor: said the screams were his mother's before Voldemort's attack (outside knowledge, text does not explain it yet); (2) para 756 dobby, Harry's Grim omen: described Trelawney's tea-leaf reading and said Harry does not die; (3) para 1868 hagrid, what is wrong with Lupin: said Snape's potion is the only thing that helps him (later fact). The outcome rule fixed the survival-denial pattern but Gemini still volunteers outside knowledge or later facts when the passages are thin. One more prompt iteration was not run (Claude budget).
Cost: rejudge ~$2.10 + rerun ~$2.11 Anthropic (Gemini answers on GCP). Results in data/eval/ (before in data/eval/before-fix/).

## 2026-10-23 — why deleting the IR textbook was slow (#64)
- The repo `.env` sets `RETRIEVAL_BACKEND=vertex`, so a *local* run also deletes from Vertex Vector Search.
- `u-irbookprint-7f5d57c8` has 1257 chunks -> 1257 single-object DELETEs (32 in flight, retries with backoff on 429/5xx).
  Loading the book + computing the ids takes ~0.1 s; removing its 5 MB folder takes a few ms. All the time is Vertex.
- Fix: the request only saves the ids to `books/<id>/deleting.json` and removes `book.json` (the card goes at once);
  the Vertex deletes and file removal run as a background task with state on `/status` (`deleting` / `delete_error`).
  A retry joins a running delete or resumes a failed one from `deleting.json`. Not done: a startup sweep for
  tombstones left by a restart mid-delete (they are invisible; a DELETE of that id resumes them).
## Avatar stream timing (offline, from 20 cached lip-sync clips, 2026-10-23, #66)
- Streamed fMP4: H.264 24 fps (timescale 12288) + AAC 24 kHz, no B-frames, tfdt starts at 0. Fragments: first 0.7927 s
  (first frame lasts ~2 frames), then 0.75 s (18 frames). Init segment `mvhd` duration 0, no `mehd`: MSE duration is
  +Infinity, so `seekable` = [0, buffered end] and a seek past the rendered part is clamped to it (Chromium
  MediaSource::SeekableInternal + HTMLMediaElement::Seek `Nearest`). This clamp caused the ~0.5 s voice rewind.
- Clips are padded at the end to whole fragments (8.40 s mp3 -> 9.04 s clip). No lead-in: the clip's own AAC track equals
  our TTS mp3 delayed by a constant +40 ms (= 1024-sample AAC priming not trimmed, no edit list). Lip motion vs voice
  envelope (rough mouth-region motion): median lag ~0 s. So no constant start offset to apply.
- Pace: ~1.5-3 s from request to first fragment (up to ~6 s with several streams in flight), then about real time
  (TTS-ready -> stream-end of 10.7 s for a 9.04 s clip). The first sentence's clip therefore cannot catch up with a voice
  that starts immediately; the voice now waits (bounded) for ~2 fragments before the first word.
- Fixed (#79): avatar seconds now come from mp3 frame headers (app/mp3dur.py), fallback len/8000 (cached mp3s are 64 kbit/s, so old len/16000 was 2x low).
## Read-mode lip-sync (#80, 2026-10-23, designed offline: no Boson calls)
- Read mode used to play only the generic talk loop, so the mouth never matched the words. Now, behind "Lip-sync while
  reading" (Stage, off by default, shown only when a face on screen is bound to a voice), ReaderEngine streams a Boson
  avatar clip per TTS chunk through the same path as Talkback: `/api/avatar/stream` (talk/clipVideo -> avatarStream),
  the start gate (talk/startGate, now shared with sentencePlayer) and FaceSync's drift control. Voice and clip start
  together from 0; the voice is never paused or rewound; a stalled clip drops to the talk loop and rejoins by rendezvous.
- Prefetch: the next chunk's clip opens the moment the current voice starts (READ_CLIP_AHEAD = 1, so at most two
  renders in flight, inside the server's per-clip lock and 12/min bucket). With Boson's ~2 s start + real-time pace a
  10 s chunk gives the next clip ~8 s of head start, so chained chunks pass the gate at once (fake-clock test: gap < 150
  ms). Between chunks the gate waits at most 1 s (READ_CHAIN_WAIT_MS); after Play/seek, 6 s. A seek, skip or voice change
  releases every clip outside [index, index+1]; a pause keeps the in-flight prefetch (aborting it would discard a paid,
  nearly finished render: the server only caches streams that complete).
- Faces: the server lip-syncs the face BOUND TO THE VOICE it is sent (`faces.voice_face_path`), and it does not check
  that the audio was made with that voice. So a chunk is lip-synced on its own voice when the face shown for its speaker
  is bound to it; a speaker with no face is read by the narrator's face (loop and clip), sent with the narrator's voice.
  Resolution goes through the reader's existing speaker -> face / voice functions (reader/readLipSync.ReadFaces), so the
  planned two-voice model only changes those.
- Cost: Higgs Avatar 640x640 (the streamed size) is $0.09/min of video (docs.boson.ai/pricing), i.e. about $0.09 per
  minute of reading; cached clips (any line read with lip-sync before, same voice) are free. The session estimate in the
  debug drawer counts fresh renders (spend.addAvatar) from the chunk's text length.
- Not measured live: the real gap between chunks and Boson's behaviour with two streams in flight for minutes at a time.

## Two voices per novel (#85, #86), 2026-10-23
- Novels now default to cast mode "two": every speaker (main cast and the minor_m/minor_f stand-ins) reads in a male or a female voice by gender; the narrator uses the one the reader picks. Resolved at TTS time (backend/app/cast.py voice_for/tags_for, frontend castLogic.castVoice), stored per user in the cast settings; book.json is untouched, "Full cast" mode restores the per-character voices.
- Gender (backend/app/genders.py, free, offline): title/noun in the name, first-name list, tag pronouns (attribute.infer_genders, >=3 and 85%), then the cast preset's gender. All 40 characters of the five classics and the 47 of HP1 + HP5 come out right (it also fixes Fred in A Christmas Carol and Mole, both cast with a female preset). Unknown -> narrator's voice.
- Character pitch/speed tags are dropped in two-voice mode, so character lines are new TTS cache keys: the first read of a book in two-voice mode re-synthesises its dialogue (narration keeps its cache because the narrator's slot defaults to the book's narrator voice). Demo mode (cached_only) needs a prerender in two-voice mode or Full cast.
- Minor pass on the classics, dry run only (not applied): Alice 205, Carol 119, Peter Pan 140, Willows 45, Oz 101 uncast lines would get a stand-in (610 total: 561 male, 49 female).

## Recap menu (#89, 2026-10-23)
- `GET /api/books/{id}/recap?para=&scope=chapter|book|series` (backend/app/recap.py). No model call: built from stored
  "story so far" summaries, all with `upto_para <= para`. "Last chapter" = the last chapter the reader finished (in the
  first chapter: that chapter so far). Its part is the text after "The current chapter so far:" when the note has that
  mark (uploads, Alice, Oz, Peter Pan, Carol, Willows); for rolling notes without it (HP) the sentences the note before
  the chapter did not already say (>= 60% content-word overlap = old), else the whole note.
- The recap is posted into the Talkback chat as a `narrator` message and read aloud in the narrator voice. It is NOT
  sent to the model and is left out of the chat memory (`priorTurns`): it is not a turn of the conversation, and the
  answer prompt already carries the spoiler-safe story so far, so including it would only spend prompt budget.

## Play/pause on every reply (#90, 2026-10-23)
- The answer being spoken shows its play/pause button from its first sentence. Pause holds it in place and the turn
  stays open (a question or Stop still cancels it): Gemini path = `SentenceSpeaker.pause()` (the clip's audio element is
  paused; faceSync freezes the face on the voice's `pause` event and resumes it on `playing`); Realtime path =
  `RingPlayer.pausePlayback()` suspends the AudioContext, so the worklet stops mid-buffer and the rest of the reply
  queues in the ring (180 s cap); a flush (barge-in/Stop) wakes it first so truncation still gets the played ms.
  A session without playback control falls back to Stop (the reply can be replayed afterwards).
- Finished replies replay; a playing replay now pauses/resumes too. One voice at a time: starting any reply pauses or
  stops the others (a paused live answer is stopped when another reply is played).
- Not changed (other agents own it): while the live answer is paused the status line may read "Thinking…".

## Resume mid-sentence (#88, 2026-10-23)
- Read-aloud (narrator recaps) pauses through `SentenceSpeaker.pause()`: the clip's audio element is paused and kept,
  and Resume plays it on from that moment. Up to 3 paused texts keep their clip; past that, or when the clip is gone
  (stopped), Resume restarts the part it was on. The lip-sync video, when there is one, pauses and resumes with the
  voice (faceSync follows the voice's pause/playing events). Chat replies (live and replays) use the same speaker pause.

## Lip-sync always on (#92, 2026-10-23)
- User decision: no toggles. Read mode, Talkback answers and recaps read aloud are lip-synced whenever the face shown is
  bound to the voice that speaks (same clip path + start gate + FaceSync everywhere; recaps use the narrator's face,
  which TalkbackView shows while a recap plays). The "talkback:lipsync" / "talkback:readlipsync" localStorage keys are
  no longer read. No price or delay copy remains in the UI.
- Silent fallbacks: no face / unbound face / failed or stalled render -> talk loop or still face. (Read mode at speeds
  other than 1x used to drop the clip source; since #105 the clip plays at the voice's playbackRate instead.)
- Cost: every fresh line spoken by a bound face now renders a clip (~$0.09/min of video, cached per line + voice).

## Voices panel: One voice / Two voices, Full cast removed (#93, 2026-10-23)
- Novels: a One voice / Two voices segmented toggle (Two voices default), a narrator pick (Male / Female voice) and the
  two voice cards (voice + face). In One voice mode the card that does not narrate is greyed out (aria-disabled, not
  focusable) and every line, dialogue included, reads in the narrator's voice; the per-character gender list shows in
  Two voices only. Textbooks: one card. The per-character voice / pitch / face rows (Full cast) are gone.
- Migration: cast mode "cast" (stored or sent by an old client) is read as "two" on the server (pydantic before-
  validator) and in the browser (castLogic.voiceMode, which also maps missing/unknown values). The `characters`
  assignments stay in cast_settings.json but novels no longer read from them, so per-character pitch tags are dropped.

## Auto title: several authors (#95, 2026-10-23)
- The describe step joins authors with ", ". Metadata / Gutenberg author fields are split on ; / | newlines, "and", "&"
  and commas ("Last, First" turned round; "Martin Luther King, Jr." kept). A field of 4+ words with no separator may
  be names run together (the IR PDF: "Christopher D. Manning Prabhakar Raghavan Hinrich Schütze"): the fast LLM, which
  sees the title page, is then asked for the list (schema `authors: list[str]`), and its answer replaces the metadata.
  When no LLM can be used the metadata string is kept as is.

## Display equations in uploaded textbooks (#41, 2026-10-23)
- PDFs keep drawn glyphs, not LaTeX, so equations extracted as garbled text ("ˆβ1 = Pn i=1..."). Now, during PDF
  extraction (ingest_upload._mark_math), a display equation becomes its own paragraph (`kind="math"`, `math: MathBlock`
  with page, bbox, number): math-font lines (CMMI/CMSY/CMEX..., Symbol, *Math) clustered by row, excluded when they
  share a row with prose (inline math stays in its sentence), and accepted only when set off (indented, taller than a
  text line, or more than the page's normal leading above and below). Pseudocode is immune. Novels never keep them.
- After publish, a background job crops each equation from the PDF (200 dpi greyscale PNG, data/books/<id>/equations/,
  served at /api/books/<id>/equations/<eq>.png without the access code since an <img> cannot send it; guard_book only)
  and, when Gemini on Vertex is configured (UPLOAD_ENRICH + GCP_PROJECT), sends the pictures 8 per call for LaTeX and a
  plain-English reading, under the upload spend caps. Locally (no GCP_PROJECT) only the pictures are made.
- Reader: KaTeX (trust off; refused \href etc. fall back) when LaTeX exists, else the crop (inverted on the dark
  theme), else the text. Spoken: one chunk, the Gemini reading ("Equation three point four: ..."), else the text when
  it extracted cleanly, else "Equation three point four." Digits after the point are read one by one (3.18 -> "three
  point one eight").
- ISLR (1st printing): 272 equations, 163 of 173 numbered ones found; misses are prose hypotheses (3.12/3.13) and
  "maximize ... subject to" problems (9.9-9.15), plus (3.27) loses its number (it sits beside the prose condition
  "if ith person is female"). The bogus "1 B" section (a fraction of (5.8) read as a heading) is gone. Crops of the
  whole book: 0.8 s, ~1 MB. Inline math inside sentences: see "Inline formulas" below.
- Books uploaded before this change have no math paragraphs; re-upload to get them.
- A new GCP project cannot call gemini-2.5-flash (404) and Gemini 3 is served only from the `global` location, so
  `.env` sets GEMINI_MODEL=gemini-3.5-flash and GEMINI_LOCATION=global (config.gemini_region). Gemini 3 rejects
  thinking_budget (400): the provider drops it and retries, and adds 8192 output tokens because the model spends the
  cap on hidden thoughts (two equations took ~1,500 thought tokens).

## Inline formulas in uploaded textbooks (2026-10-23)
- Inside a prose line, a run of math-font spans (plus the operators, digits, brackets and operator names such as
  "max" between them) is marked when its text would read wrong: lost glyphs, accents (ˆ ¯), big operators or roots,
  or sub/superscripts (a span < 85% of the run's size or with a shifted baseline). A plain italic x is left alone.
  Detection uses fonts and sizes only, nothing book-specific. Leading prose punctuation/numbers ("3.1,", "•") and
  trailing sentence punctuation stay outside the formula.
- The marker rides in pdf_to_text's output (parsing.IM_OPEN + page/bbox as private-use chars + text + IM_CLOSE) but
  only on the final paragraph text: layout heuristics (headers, headings, code, TOC) see the unmarked line. parse()
  turns markers into `Paragraph.inline_math` (offsets into the text); long-paragraph splits never cut one; novels
  drop them.
- After the display equations, the same job crops each distinct inline formula (same text and size share one
  picture) and Gemini reads them in the same batches; the inline pass is trimmed to fit the per-book cap on the same
  LLM instance. The reading replaces the garbled text in the TTS chunk (packing measures the spoken length; readings
  over 120 chars are not used) and its spoken words highlight the formula (tts_cache.align_timestamps `inline`).
- Reader: KaTeX in text style inside the sentence, else the crop sized by its PDF height, else the text.
- Crops (display and inline) paint out other lines' text: math fonts report boxes much taller than their glyphs
  (CMSY "≈" reaches 11pt below its baseline), so a padded box caught the tops of the next line. A span whose baseline
  is outside the box is whited out over base-0.78*size..base+0.25*size (not its font box, which can reach above the
  formula's baseline); prose beside an inline formula is whited out up to the box edge. The inline box's baseline is
  the lowest main-size baseline (the hat of ˆβ sits on a raised one). Picture URLs carry ?v=<hash of the PNG>
  because the route is cached for a day.
- A root, sum or hat is often its own PDF block, with a font box tall enough to overlap the lines above and below,
  so the glyph used to fall into the next sentence ("!n where", "RSE = { RSS"). Rows on one baseline are joined, and
  a math-only row is moved onto the prose row it actually sits in (the gap between the words, not the line above
  whose words it overlaps). "SE" / "RSE" with no lowercase letter count as part of that formula. The display-equation
  crop stops at the next line's letters, so the sum's box no longer takes in "where". An inline crop also whites out a
  word whose baseline falls inside a tall sum or root ("we can" on the sum, "known as the" on the radical).
  A display equation's tall brackets report a baseline near the sentence above; they are placed by the middle of
  their ink, so they stay on the equation and "a 95% chance" is left as a sentence. Once a glyph is appended, that
  sentence stays a prose row: reclassifying it deleted the sentence and the next glyph crashed extraction
  (`TypeError` on the CQL paper; short excerpts never hit a second glyph on the same line).
- A paper the heuristic calls a novel is stored with formulas flattened to text (only textbooks keep them). Moving
  that PDF to Textbooks reads the file again and crops the pictures; the kind flag alone left the flat text in place.
  Merged onto the locked kind change: the PDF is read outside the write lock, then sections/paragraphs are put into a
  fresh read under `books.update_book` (other writers' fields survive), summaries are dropped (old paragraph numbers)
  so the textbook notes run under the caps, chunks are rebuilt with `rechunk_book`, and the retrieval index is rebuilt
  like an upload's (local chunks; Vertex: stale datapoints deleted, new chunks indexed in the background). Kept
  characters keep their old `first_para`, and paragraph numbers change, so a saved reading position may shift.
- An inline crop's height is the picture. The padding that keeps a KaTeX limit off the next line was also on the
  image, and with border-box it shrank the picture to about two thirds. A tall delimiter is placed by the middle
  of its real box: a band around its reported baseline (the top of the glyph) glued the brackets of the equation
  below into the sentence, and that crop showed up as a tiny second copy of the line.
- ISLR excerpt: 184 inline formulas (79 distinct crops), all read (e.g. "\hat{\beta}_1", "beta one hat"). CQL paper:
  49. Limits: a formula that pymupdf splits over several text lines (a stacked sum's limits, a hat set on its own
  line) is marked only in part; ligature glyphs such as "coe!cients" are a separate extraction issue.
- Pre-existing, not from this change: number expansion ("6,130" -> "six thousand one hundred thirty") runs after
  the 280-char packing, so number-heavy chunks (tables, prices) reach ~500 chars. Fixing the packing would re-chunk
  the demo novels and invalidate their pre-rendered audio, so it is left for a separate change.

## Voice agent: Realtime tools act on the reader (#72, 2026-10-23)
- `frontend/src/talk/tools.ts` declares ten client function tools to `session.update` (`tools[]`, `tool_choice: auto`):
  jump_to (chapter number / title / Roman numeral / "last" / "bookmark", or a quoted passage), read_chapter, resume_reading,
  pause_reading, set_voice_mode (one/two), set_narrator (male/female), set_speed (1.25 / faster / slower / normal),
  open_recap (chapter/book/series), bookmark, show_cast. Boson's server-side `{"type": "web_search"}` is never declared
  (novel spoilers). `runTool` is pure: a `ToolContext` snapshot + `ToolActions` (Reader.tsx wires seek/pause/cast/speed/
  recap/Voices panel), so every tool is unit-tested without a DOM.
- Flow (TalkClient): `response.function_call_arguments.done` is remembered; `response.done` with a `function_call` item
  whose `executor` is `"client"` (server-run tools are skipped) becomes a `tool-call` session event instead of a finished
  answer; TalkController runs the tool, `returnTools()` sends `function_call_output` per call and a `response.create` with
  the same per-response instructions, and the model speaks the confirmation. A cancelled response drops its calls.
- Spoiler guard: `resolveJump` refuses any target whose first chunk's paragraph is past `readerPara` when spoilers are off
  (a quoted passage lands on the nearest match at or before the reader; bookmarks set with spoilers on are checked too).
  The model is told why in the tool output so it can say so; the reader never moves.
- After the answer: a tool that moved the reader sets where auto-resume continues (read_chapter / resume_reading force
  a resume even with auto-resume off and switch Talkback back to the page; pause_reading stops the resume). A chip
  ("Jumped to Chapter II") is a `role: "tool"` chat message, kept in the log, never sent back as history.
- Gemini answer path (no Realtime, so no function calling): `parseIntent` recognises a few plain phrasings ("read chapter 3",
  "jump to the part where ...", "keep reading", "pause", "a bit faster", "one voice", "recap the last chapter", "bookmark
  this", "show me the cast") and runs the same tool without calling Gemini; anything else is a question. Refusals toast.

## Barge-in: interrupt a character mid-answer (#10, 2026-10-23)
- Realtime: the mic press (or Space, or a typed question) while an answer plays does, in this order: local playback
  flush (the audible stop, well under 0.15 s), `conversation.item.truncate` at the ms actually heard (`audio_end_ms`),
  `response.cancel`, wait for the cancelled `response.done`, then open the mic. Already in TalkClient.interrupt; now
  covered by fake-socket tests (test/bargeIn.test.ts). Gemini + TTS: the press aborts the stream and stops the
  sentence clip (lip-sync included); no socket traffic.
- The cut-off bubble is marked `interrupted` ("· interrupted", an em dash after the text), and `priorTurns` appends
  " [the reader interrupted here]" to that reply, so the next answer (both paths) knows what was not heard.
- Hands-free (off by default, Talkback view button): a client-side voice gate (talk/handsFree.ts) over the capture
  worklet's RMS. Speech over 0.04 RMS for 150 ms starts a turn (barging in if the character is talking); 700 ms under
  it commits the turn; a gate turn is never a tap. The mic stays open between turns (`TalkClient.setMonitor`), keeps
  the last ~300 ms and sends it first so the first word is not clipped. Tuned for laptop mics; echo cancellation is on,
  headphones recommended.
- Why not Boson `server_vad` / `semantic_vad`: the migration guide says `create_response`/`interrupt_response` are
  accepted but have no effect, i.e. the server always answers on end of turn by itself. That answer would not carry the
  per-response retrieval instructions (spoiler-safe context, persona), so turns stay manual (`turn_detection: null`).
  The session still sets `noise_reduction: near_field`. If Boson adds `create_response: false`, `server_vad` with
  `threshold` ~0.6-0.7 (its default moved to 0.85) would replace the local gate.
  **Superseded by #135 (below): `create_response: false` is honoured live, so hands-free now uses `semantic_vad`.**

## Spoken turns live: tool calls, barge-in leftovers, hands-free echo (#102, #97, #99, #100, #103, 2026-10-23)
- Spoken commands failed where typed ones worked because the *question* was lost, not the tool. `pttUp` waited a fixed
  2.5 s for the transcript from the key release; on the first turn of a session the commit is deferred until the socket
  opens (key fetch + connect), and STT runs after that, so the window expired, the query was "" and it still went to
  Gemini (`/api/answer` -> "The answer could not be generated") or to retrieval. Now `beginTurn` keeps the
  ensureOpen/interrupt promises, `pttUp` awaits them, the window (6 s) starts at the real commit, an empty or failed
  transcript ends the turn with "I didn't catch that" and never asks an empty question (#99 as well).
- `parseIntent` strips transcription prose ("Okay, read chapter 2, please!" / "Could you ...?") before matching.
- Realtime: every `returnTools()` re-arms the 15 s answer watchdog (a tool call's continuation is a new response that can
  go missing); only the latest watchdog acts. A typed question is sent as a user `input_text` item before
  `response.create` (#97): the instructions carry the passages, not the question.
- Barge-in (#100): the old answer's `output_audio.delta` / caption deltas keep arriving after the local flush (the server
  runs ahead of real time); TalkClient drops all output from the interrupt until the next `response.created`. The mic
  starts at once; audio and the commit are held in the client until the cancel is acknowledged (`response.done` or
  `response_not_active`), then sent in order. A cancel never acknowledged fails the turn with a message.
- Boson's AssistantItem content takes `{type: "text"}`; `output_text` (history seeding) was rejected with 22 pydantic errors.
- Hands-free echo (#103): `getUserMedia` asks for echoCancellation + noiseSuppression + autoGainControl. While the
  character speaks (and 300 ms after) the gate uses `HANDS_FREE_OVER_SPEECH` (0.10 RMS for 400 ms) instead of the idle
  profile (0.04 for 150 ms): residual echo and a cough do not interrupt, clear sustained speech does, the mic button /
  Space always does. The toggle says "Hands-free (use headphones)".
## Short chunks: voice before face (#104, 2026-10-23, fake-clock tests, no Boson calls)
- Cause (read mode): (1) when a chunk's voice ended, FaceSync kept the old clip attached and SHOWN, frozen on its last
  frame ('pause' arrives before 'ended'), until the next clip joined; (2) the next clip opened only when the current
  voice started (1 ahead), so a ~1 s chunk gave it ~1 s of head start against Boson's 1.5-3 s first fragment + ~0.8 s
  pre-roll, and the 1 s chained gate timed out: the next voice started under the previous chunk's still face, and its
  own clip joined late (rendezvous) or never. Talkback was not affected the same way (its speaker leaves the sync at
  each sentence end and waits up to 6 s per sentence).
- Fix: FaceSync hides the face on the voice's `ended`; ReaderEngine leaves the sync when a chunk ends; clips open ahead
  until the next unopened one has >= 4 s of speech as head start (max 3 ahead, readLipSync.clipAheadFor); chained wait
  1 s -> 3 s. More renders in flight with short chunks: the server's 12/min avatar bucket can still refuse some (talk
  loop then).

## Lip-sync at 0.75x-1.5x (#105, 2026-10-23, fake-clock tests)
- The clip is rendered at the voice's natural pace, so it simply plays at the voice's playbackRate: FaceSync and the
  pre-rendered clip follower set video.playbackRate = audioRate * (1 +- drift nudge); AudioClock advances at the voice's
  rate; the voice keeps preservesPitch. Speed changes mid-clip are followed on the next tick.
- Start gate at speed: a live stream renders at ~real time but is played faster, so the head start grows to
  1 - 1/(1.1 * rate) of the sentence (~39% at 1.5x, ~0.7 s base); at 0.75x only the base. A long chunk at 1.5x
  therefore waits longer before its first word, and if the render still falls behind the face drops to the talk loop
  and rejoins (unchanged fallback). Talkback answers and recaps have no speed control, so they are unaffected.


## Lip-sync for any voice x face (#113)
- Before: lip-sync requests sent only the voice and the server animated `faces.voice_face_path(voice)`, so only a
  cloned voice with its own face ever lip-synced; uploaded faces, presets with a face and newly cloned voices with a
  picked face fell back to the talk loop. Now every request (answers, recaps, read mode per chunk, replays re-open the
  same clip) carries `face_id` = the face on screen; `_lipsync_face` resolves it in the caller's library (404 for
  another account's or unknown id), and falls back to the voice's face when no id is sent. Cache key unchanged
  (audio key + face image hash), so old clips stay valid.
- Cloning with an existing face sends `face_id` to `/api/voices/clone`: `faces.link_voice` marks the entry
  `voice` + `linked: true` (no copy). Deleting the voice deletes only faces made with the clone (no `linked`) and
  unlinks borrowed ones (`faces.unlink_voice`). The old `face` file upload still works.
- Realtime (Boson realtime) answers are audio only, as before; nothing to lip-sync there.

## Questions in other languages (#12, 2026-10-23)
- Answer language: the prompts already said "Always reply in the language the reader used", but deep in a long English prompt. When the question is clearly not English (`multilingual.foreign_language`: a non-Latin script, or 2+ common words of one language outnumbering English ones), `build_instructions(reply_language=...)` now opens the prompt with "The reader wrote in Spanish. Answer only in Spanish ..." (first, so the length cap never cuts it). Gemini (`build_answer_prompt`, also the public service) sets it from the question; Realtime gets it in the `/api/retrieve` instructions (typed and spoken questions). The model still sees the reader's own words.
- Search: `multilingual.search_query` translates a non-English question to English with Gemini on Vertex (temperature 0, thinking off, 4 s timeout, 256-entry cache) before `/api/retrieve` and `/api/answer` search; `/api/retrieve` returns `search_query` when it differs. Without GCP_PROJECT, or on any failure, the original question is searched.
- Why it matters: with BM25 alone (local, no e5 installed; also Vertex books while their index builds) a Spanish question finds nothing relevant. Alice: "¿Qué le pasa a Alicia cuando bebe de la botella?" returned ¶114-118 / 141-142 / 215-221 (unrelated); its English form returns ¶14-17 and ¶109-111 (the bottle scenes).
- Not live-tested here (this machine has no GCP_PROJECT, and a Realtime check spends Boson credit). The badge (frontend/src/talk/language.ts) uses the same word lists; voice commands (#72 parseIntent) are still English only.
## More expressive read-aloud tags, rule-based (#73, 2026-10-23, offline: no Boson calls)
- `backend/app/delivery.py` (no LLM, strict; ambiguous -> no tag). Every sfx goes right before onomatopoeia already at the
  start of a character's line (after the opening quote); narration never gets one. `speech_tag()` is the short adjacent
  speech tag (same <=9-word clauses the style rule uses).
  - sigh: Sigh / Ahh / Ahh-h / Haah, only if the tag says sighed/sighs/sighing.
  - crying: Sob / Boo-hoo / Boo hoo, only if the tag says sobbed, wept, weeping, crying (not "crying out"), in tears,
    or "cried bitterly/softly/quietly/piteously/harder/silently/herself...". Plain "cried Alice" means said -> no sfx.
  - sniff, cough: Sniff / Cough when followed by punctuation or repeated ("Sniff this rose" is a verb); Ahem -> cough.
  - screaming: Aah+! / Aaargh! with "!", and either uppercase (AAAARGH!) or a screamed/shrieked tag. Lowercase "Aaah!"
    alone is not tagged (HP4: Mr. Weasley's "Aaah!" is delight).
  - laughter unchanged.
- Expressiveness: `<|prosody:expressive_high|>` for a character line that is mostly uppercase with "!", or ends with "!"
  and its tag says shouted/screamed; `<|prosody:expressive_low|>` for a whispered line (alongside style:whispering).
  Never added when the voice tags already carry an expressive_* tag.
- Pauses (`ttschunks.lead_pause`, first chunk of a paragraph only, right before the text): `long_pause` for the first
  paragraph of every section (after the heading, which is not spoken) and after a separator paragraph (`* * *`, `***`,
  `#`, `~ ~ ~`; the separator's own chunk is unchanged); `pause` for a dialogue paragraph right after a paragraph of
  pure narration that ended with . ! or ? (narration ending in ":" or "," introduces the quote, so no pause).
- tts_text order: voice tags, style, emotion, expressive_*, pause/long_pause, text (sfx inside the text).
- Chapter-end reset tag: not done. Neither the Boson vocabulary we recorded (above, and delivery.TAG_VALUES) nor the
  tags page lists a reset tag, so nothing is inserted.
- Timestamp residue: all new tags are already in TAG_VALUES, so `strip_tag_residue` removes 'prosodylongpause',
  'prosodyexpressivelow', 'sfxcrying' etc. glued to words (tests in backend/tests/test_expressive.py).
- Cost: existing book.json files are NOT rebuilt, so cached audio is untouched; the tags appear when a book's chunks are
  rebuilt (preprocess/chunk_tts.py, re-enrich, new uploads). `scripts/estimate_expressive.py` (offline) compares
  chunk_paragraph(expressive=False) with the new rules per book. Run on the main checkout's data/books:
  9642 of ~104k chunks change, 806k chars, $12.09 if every changed chunk were re-voiced (HP1-7 $10.15, five classics
  $1.09, IR textbook $0.85); only 16 changed chunks ($0.05) have audio in the local cache under the book's own voice.
  `pause` makes up ~85% of the changes; real sfx hits are rare (HP1-7: 8 cough/screaming, classics: 0).
## Upload parity (#115)
- Audit of built-in vs uploaded books: uploads already match on cast, aliases, first_para, emotions/styles, stand-ins,
  summaries and index. Gaps fixed: the #17 attribution pass now always runs after segmentation (it ran only inside the
  MINOR_VOICES pass); the cast call also returns `series` / `series_index` (slugged, e.g. "harry-potter"; a series
  already set is kept); `books.earlier_in_series` only joins books of the same library (built-in with built-in, an
  upload with the same owner's uploads).
- Left as proposals: textbook summaries (~$0.05 per 100K chars, IR book ~$1.60), personas from earliest spoken lines
  (+~$0.004/novel, changes the spoiler window), resegment with a stronger tier, PDF cover image (free).

## Singing songs and poems: `<|style:singing|>` (#74, 2026-10-23)
- Probe (2 Boson calls, ~$0.004): higgs-tts-3, preset `marcus`, the Hatter's 'Twinkle, twinkle, little bat' with and
  without `<|style:singing|>` at the start (same request as `BosonClient.create_speech`). The tag is not spoken (timestamp
  words are the same 22 words) and the sung take is 10.4 s against 7.0 s spoken. Listen before trusting it.
- Rules (`delivery.is_sing_lead_in`, `verse_score`, `sung_from`; wired in `ttschunks.sing_offset` / `chunk_paragraph`):
  a song starts after a lead-in whose last sentence mentions sing/sang/sung/song/chorus/lullaby/hum/carol/croon and ends on
  ':' or ':—' (or on the verb 'sing'/'sang', or is a 'CHORUS.' line), with no repeat/recite/read and no negation before
  the word. 'sang out' and 'sing-song' don't count; a bare dash doesn't either (HP5 Quidditch commentary). The next
  paragraph is sung if it opens a quote (or reads as verse) and no narration follows a closing quote; the song goes on
  while each paragraph reopens a quote the previous one left open, or reads as verse (>= 2 capitalised small words
  mid-sentence, i.e. flattened line starts). Inline lead-ins ('the hat began to sing: "Oh, you...') split the segment
  at the quote. Caps: <= 700 chars per paragraph, <= 8 paragraphs per song, same chapter only.
- A sung chunk gets voice tags, style:singing (replacing whispering/shouting), emotion, pause, text: no sfx and no
  expressive_*. `chunk_paragraph(expressive=False)` never sings.
- Recited verse is left alone ('How doth', 'Father William', ''Tis the voice of the Lobster').
- `scripts/estimate_expressive.py --list-sung` lists what would be sung. Main checkout data: Alice 10 paragraphs (13
  chunks: lullaby + two choruses, Twinkle bat, Lobster Quadrille, Beautiful Soup), Peter Pan 5, HP1 4 (Sorting Hat),
  HP2 1, HP4 1 (egg song), others 0. Known misses: Sorting Hat's last 3 short stanzas, Twinkle's second half (lead-in
  'in this way:—' has no singing word), Toad's song (lead-in ends on a bare dash), 'Weasley is our King'.
  No book.json was rebuilt: songs are sung after a book's chunks are rebuilt (preprocess/chunk_tts.py) and in new uploads.

## #78 Natural reading speed (prosody speed tags)
- Opt-in "Natural speed" checkbox in the player bar (default off, localStorage `talkback.naturalSpeed`; hidden in demo, which only plays pre-rendered audio). Off = playbackRate exactly as before.
- On: 0.75x -> `<|prosody:speed_slow|>` (~0.85x), 1.25x -> `speed_fast` (~1.2x), 1.5x -> `speed_very_fast` (~1.4x); 1x has no tag; tagged audio plays at 1.0. Boson docs (tags page): speed is a delivery token, put at the start; a new delivery group replaces the previous one.
- Backend: `POST /api/tts` takes optional `speed`; `delivery.apply_speed` puts the tag first (replacing a voice-tag speed) so the tts_text, hence the cache key, differs: normal audio is never overwritten. Lip-sync clips are keyed by audio url, so they are rendered per tag (extra cost, hence the opt-in and the UI hint).
- Engine: `setSpeed(speed, tag)`; on a tag change only entries outside the new tag are dropped and only the window [current, prefetchEnd] is fetched again. The chunk playing now finishes in its own audio (rate matches that audio); the change applies from the next chunk.
- Live check: 1 Boson call (marcus, `speed_fast`, 1 sentence) returned audio; saved in the session scratchpad speed/fast.mp3 (3.04 s for 11 words).
- Not covered: very_slow is unused (no reader speed maps to it); the speeds still 1.1/1.0 etc. use playbackRate.
## Emotion in answers (#14, 2026-10-23)
- Typed questions (Gemini, local `/api/answer` and the public service): `personas.ANSWER_EMOTION_RULE` (in `ANSWER_SUFFIX`, so after the #12 language rule and never cut by the length cap) asks the model to start every sentence with its feeling in square brackets, one English word from `schema.EMOTIONS` (the 20 values the books use; also in a non-English answer), or `[neutral]`. `app/emotion.py` `split_marker` removes the leading marker and keeps it only if it is in that list (`[Sadness]`, `[emotion:fear]`, `<|emotion:fear|>`, full-width `［surprise］` work; `[neutral]`, unknown, translated (`[tristeza]`, `【悲伤】`) and `arousal` give none). It also removes an unclosed leading `[word ` and any stray one-word `[marker]` / `<|...|>` inside the sentence (a CJK answer with no space after 。 arrives as one "sentence"), so no marker reaches the screen, the chat history or the TTS. Side effect: a genuine one-word bracket like "[sic]" is dropped from an answer. A marker-only sentence (a trailing "[relief]") is dropped. Markers split across stream chunks are fine: `gcp/llm._split` only emits whole sentences.
- NDJSON `sentence` lines now carry `"emotion"` when there is one (neutral: no field). The proxy of the public service re-validates the service's `emotion` and strips markers again (an older or misbehaving service cannot inject one). `tag_sentences` closes the inner answerer when it is closed, so token usage is still recorded on abandon.
- Frontend: `streamAnswer` passes the emotion to `onSentence`; `useTalk` keeps it per sentence text (`talk/answerEmotion.ts`) and `speak(text, voice, emotion)` sends it. `/api/speak` validates it and sends `<|emotion:X|>` + text to Boson (timestamps still align to the plain text); the cache key includes the tag, so neutral audio already cached is untouched.
- Boson check (2 TTS calls, ~110 chars, voice marcus, audio in the session scratchpad `emotion/neutral.mp3` and `sadness.mp3`): "I waited all night, and he never came back." neutral 2.48 s, with `<|emotion:sadness|>` 2.88 s, timestamps without tag residue. The delivery changes; not judged by ear here.
- Spoken questions (Boson Realtime, audio only, no tags): `/api/retrieve` instructions get `REALTIME_EMOTION_RULE` ("Let the feeling of what you say show in your voice ...") right after the language rule (`build_instructions(voice_emotion=True)`).
- Reader's emotion: Boson does not report it. The Realtime server events (`conversation.item.input_audio_transcription.completed`: item_id, content_index, transcript; `speech_started/stopped`: timing only) carry no emotion, tone or sentiment, and there is no separate STT model in the docs. So nothing is inferred from audio; the character prompts (spoiler-safe and full-book) gain `READER_TONE_RULE`: notice the reader's mood from how they word the question and react to it. Spoken questions reach Realtime as audio, so it can hear the tone itself.
- Avatar face: no expression control. `POST /v1/videos` takes model, ref_image, input / input_tts, size; avatar realtime sessions take model, avatar_id, transport, output, max_duration_seconds. The docs say lip sync, head motion and expression follow the driving voice, so the emotional TTS above is the only lever (an emotional sentence may animate the face more; not verified). Nothing changed for the avatar.
- Book text emotion tagging is unchanged (no re-enrichment, no new cost).
## Read-mode mic: face and chat memory (2026-10-23)
- Face did not move for a read-mode spoken answer: `faceSync` keeps a read clip "shown" when its voice pauses (it freezes, to resume in step), and the mic pauses the reader, so the frozen frame of the reading clip (`ReadClipVideo`, on top of the medallion in `Stage.tsx`) covered the answering FaceView for the whole answer; a chunk's pre-rendered avatar clip took precedence the same way. Talkback has no reading clip, hence fine there. Now `readFace.answerOnStage(state, speaking)` (listening..answering or still sounding) makes the Stage show the Talk face (FaceView, `talk.speaking`, `talk.faceSync`: same as Talkback) and hides the reading clip; when the answer ends the talk loop goes idle and on resume the reading face/clip come back.
- Chat memory: read-mode spoken questions and the Realtime answer transcript (`output_audio_transcript`) were already logged and given back (same session; a new session is seeded with `priorTurns`; Gemini gets `history`), now covered by `test/readModeMemory.test.ts` (fake socket). Gap fixed: the Realtime session was shared across a speaker switch (mid-session voice change), so one character heard another's chat, and a character whose turns were in an earlier closed session got none of them. After a speaker switch the session is now dropped once no answer is in progress; the next press opens one seeded with that character's turns (one extra connect after a switch, no extra paid audio).
- The "remembers this chat (N lines)" count now shows what is actually given back (last `HISTORY_TURNS` = 6 lines of that speaker), not every line.
## Textbook voice agent (2026-10-23)
- Tools by book kind (`talk/tools.ts` `toolDefsFor`, passed by `useTalk` to `session.update`): a textbook gets `jump_to` / `read_chapter` (arg `section`), `next_section`, `previous_section`, `go_back`, `find_topic`, `quiz_me`, `explain_here`, resume/pause, speed, recap, bookmark; no `show_cast`, `set_narrator`, `set_voice_mode`. Novels keep their list plus `go_back`. Session instructions shortened to a generic tool sentence (test caps them at 300 chars).
- No spoiler gate in textbooks (`ToolContext.kind === "textbook"`, a textbook is read out of order); novels keep `para <= readerPara` unless spoilers are on, for every tool including next/previous, find_topic and quiz_me.
- Sections (`talk/sections.ts`, pure, no API): by number at any depth from the title ("2.3", "two point three", "section 3.2"); a number missing from the titles that falls in a numbering gap with exactly one unnumbered section takes that section (the IR book's "2.3" heading lost its number in the PDF parse: "Faster postings list intersection via skip pointers"). By title words with a plural stemmer; ties (every "References and further reading") go to the next one from the reader, else the last before.
- `find_topic`: a title with every query word wins, else tf-idf over the read-aloud paragraphs (code skipped) with phrase and section-title bonuses, earliest paragraph on ties. Done in the browser (the whole book is already loaded; the tools are synchronous) instead of a backend endpoint: the backend index is hybrid with embeddings (a paid query embed) and spoiler-filtered. On the real IR book: "tf-idf" -> 6.2.2, "edit distance" -> 3.3.3, "Porter stemmer" -> 2.2.4 text, "BM25" -> 11.4.3, "permuterm index" -> 3.2.1.
- `go_back`: no args restarts the current paragraph; `paragraphs: n` starts n paragraphs earlier; `sentences: n` goes back n TTS chunks (sentence-aligned); always reads on.
- `quiz_me` (#75 light): returns the section's text (default the reader's section, or "this chapter", a number, title words; equations as LaTeX + spoken form, figures as captions, code skipped; capped at 3000 chars) and the rules: 3 questions one at a time, right/wrong with a one-line reason, score, offer to read on. Reading stays paused. No backend LLM call; the Realtime model runs the quiz. Quiz mode (`TalkController.quiz`): the next 3 turns send the normal grounding + `quizTurnInstructions` (rules, section text, "answered question n of 3", ask the next / give the score); progress is counted client-side per turn, the model's grading is not parsed. It ends after the 3rd answer, on stop / cancel / "keep reading" / any parseIntent command (`endsQuiz`), on any other tool call, a speaker switch, a view switch or a new book. Gemini path: the answer is sent as `quizGeminiQuery` (<= 500 chars; the questions are in the chat history). Not verified live.
- `explain_here`: the current paragraph (or the nearest equation / figure from 6 paragraphs back to 2 ahead) plus the paragraph before, explain in 2-4 sentences, offer to read on; reading paused.
- Gemini path: a tool may return `ask` (<= 500 chars, the `/api/answer` query limit); `TalkController` then asks Gemini that instead of ending the turn silently (quiz / explain).
- parseIntent fast paths (English): "read section 2.3", "go to 4.1", "read the section on X", "next section/chapter", "skip this section", "go back a section/chapter", "say that again", "repeat the last paragraph/sentence", "go back a bit / two paragraphs / 3 sentences", "where is X covered/explained", "which section covers X", "find the section on X", "quiz me (on chapter 3 / this chapter)", "explain this figure", "what does this equation mean", "explain that". Tests: `test/textbookTools.test.ts`.

## Textbook recap notes (2026-10-23)
- Textbooks used to skip enrichment, so their Recap menu was empty. Now an uploaded textbook gets study-guide notes
  (`backend/app/enrich/textbook.py`, Gemini "fast" tier): one per chapter (level-1 section) and per numbered
  subsection (level 2; level-3 headings fold into their subsection). 3-6 sentences, key ideas, definitions and formulas
  in words, only from the given text. A chapter with subsections is written from its opening text plus its
  subsections' notes, so the book is read about once. Display equations are read as their spoken form / LaTeX.
- Stored as ordinary `Summary` rows with a new optional `section_id`; `upto_para` = the section's last paragraph, so
  each note is still derived only from text at or before `upto_para` (safe even if the book is moved to Novels).
- Recap (`recap.py`): a textbook with section notes has no spoilers. "chapter" = the note of the chapter the reader is
  in (menu label "This chapter"), `section=<id>` = any chapter / subsection wherever the reader is; "book" = the first
  two sentences of each chapter's note up to the reader's chapter (300 words, latest kept). A textbook without
  section notes and every novel keep the old rule; a novel's `section` recap still uses only `upto_para <= para`.
  Voice/typed "summarize chapter 4" / "summarise section 4.2" -> `open_recap {chapter}` (frontend `tools.ts`); the
  Realtime tool got an optional `chapter` argument. Novels refuse a chapter past the reader while spoilers are off.
- Flow: same background job as novels (`main._run_enrichment` dispatches on kind; `_enrich_lock`), same estimate first,
  per-book cap, total cap (data/usage/upload_spend.json), SpendLimit stop (notes written before a stop are kept), and
  UPLOAD_CONFIRM. Status carries `summaries_only: true` with textbook wording. Subsections are left out only when the
  full estimate is over the per-book cap. The save re-reads book.json under the equations lock (that job may write
  LaTeX meanwhile). Moving a novel to Textbooks writes notes only when it has no summaries at all (its story notes are
  kept); moving a textbook to Novels runs the novel enrichment as before.
- Public service: `/api/public/enrich` accepts `kind: "textbook"` (sections carry `level`) and runs only the notes;
  `public_client.summarize_textbook` keeps only notes for real sections, with `upto_para` reset to the section end.
  An older deployed service answers 422 -> the textbook simply has no notes. Needs a redeploy of talkback-public.
- Existing books: `scripts/summarize_textbook.py --book <id>` prints the estimate and caps; `--yes` runs it (refused
  over a cap even with --yes). IR textbook (1.12M chars, 32 chapters + 118 subsections, 150 calls), computed offline
  from the raw JSON: ~$0.33 with subsections, ~$0.16 chapters only (margin included; Gemini 2.5 Flash prices). On this
  branch the IR book.json fails schema validation ('figure' paragraphs from the figures branch), so run the script
  after that branch is merged. Not run against Gemini here (fake LLM only).
## Chunk-rules version: books pick up new TTS delivery rules by themselves (2026-10-23)
- `ttschunks.CHUNK_RULES_VERSION` (now 1 = #73 expressive delivery + #74 singing) and `Book.chunk_rules` (default 0,
  so every existing book.json still validates). `ttschunks.rechunk_book` rebuilds all chunks and stamps the version;
  ingest (`ingest_upload.build_book`, `ingest_series`), enrich (`pipeline.rebuild_chunks`, `minor`), voice baking and
  `preprocess/attribute.py` / `chunk_tts.py` all go through it. **Bump the constant whenever chunk/tag rules change.**
- Server: `books.load_book` queues a re-chunk of an out-of-date book on one background thread
  (`books._upgrade`), and `main._warm_up` queues every book at startup (`books.schedule_all_upgrades`). Requests keep
  the old chunks until the new file is saved; the next `load_book` sees the new mtime/size and returns a new object,
  so `main._book_bodies` re-serialises. Measured: Alice 1812 chunks 0.28 s (171 chunks change), HP5 18k chunks 2.9 s
  (1836 change); styles unchanged (Alice 17 shouting / 12 whispering before and after).
- Safety: works on a fresh parse (not the shared cached object, so no portrait URLs get written); one upgrade per file
  at a time (in-process dedupe); skips the write if the file changed since it was read (an enrich save wins); any error
  (bad segment, read-only disk) is logged, the old file stays, and that file version is not retried. Segment styles
  are never cleared. Two Cloud Run instances can both rewrite a book once; the content is identical. `AUTO_RECHUNK=false`
  turns it off (tests do, so they never rewrite the committed data/books).
- Side effect: running the server rewrites out-of-date committed `data/books/*/book.json` in a clone (git shows them
  modified) until the rebuilt files are committed. Rebuild offline with `preprocess/chunk_tts.py --book <id>`.
- `preprocess/chunk_tts.py` now keeps segment styles by default (it used to clear them, which dropped Alice's
  LLM-assigned styles, 17->7 shouting, 12->7 whispering); `--redetect-styles` keeps the old behaviour.
- Cost: TTS cache keys are sha256(model, tts_text, voice, ...), so only chunks whose tts_text changed are re-voiced,
  and only when played (same for lip-sync videos). Unchanged chunks keep their audio (tested).

## Public API parity for clones (2026-10-23)
Audit of every Gemini/Vertex feature on a clone with only a Boson key + PUBLIC_API_URL (code on feat/public-api-parity;
the deployed talkback-public-00001 is the Oct 21 code):
| Feature | Clone before | Clone now | Old deployment |
|---|---|---|---|
| Novel cast, personas, dialogue (#115 attribution), summaries | works | works | no #115 attribution changes |
| Series / series_index | missing (not returned) | works | missing |
| Minor voices (stand-ins) | degraded (stand-ins made main, cut at 8) | works (kept main=False, fixed voices) | missing |
| Textbook recap notes | works | works | missing (422 -> no notes) |
| Equation LaTeX / spoken (#41) | missing (local GCP only) | works (tickets) | missing |
| Question translation for search (#12) | missing | works (<= 4 s, else original) | missing |
| Reply language rule | works (built server-side from the query) | works | missing (older prompt) |
| Emotion markers in answers (#14) | works (stripped both sides) | works | server sends none; client strips |
| Typed answers via Gemini | works | works | works (history ignored) |
| Face auto-crop box (#109) | missing (centred square) | works | missing |
| Add book title/author (describe) | degraded (metadata only) | works | missing |
| Novel/textbook auto-detect | degraded (heuristic) | works | missing |
| Retrieval embeddings (local e5) / Vector Search (live only) | fine | fine | n/a |
- New routes in `public_api.py`: `/api/public/translate`, `/detect`, `/describe`, `/faces/detect` (one call each,
  counted on the persisted daily answer budget; face boxes also on a persisted 100/day count), and
  `/api/public/equations` (ticket: fits the pictures under the per-book cap, checks the total cap with running
  reservations, charges the per-IP daily cap) + `/api/public/equations/{ticket}` (<= 24 PNGs, 256 KB each, 2 MB total,
  bound to the caller's address, two at a time, 90 s; a cap stops with `"stopped"` and keeps the readings).
  `equations.read()` is the shared batch reader. Body limits per route in `BodyLimit`. No content is logged.
- Clone side (`public_client.py`): `translate`, `RemoteLLM` (detect/describe keep their own schema validation),
  `detect_face` (re-clamped), `transcribe_equations` (display first, chunks by count and bytes, retries busy, readings
  re-cleaned and limited to the ids sent; note says why pictures stay). A 401/403 marks the service unavailable for a
  minute (answers then use Realtime too). All fall back exactly like the local no-GCP path.
- No new env vars for deploy_public_api.sh (limits are constants). Needs a redeploy of talkback-public.
- Security review fixes (equation tickets): one running request per ticket (`busy` -> 429), spend added per call
  (`t.usd += usd`, never overwritten), settle/close/purge never while a request runs; one open ticket per IP (429
  `ticket_open`), 32 in all, a ticket with no picture expires after 2 minutes, and every ticket costs its address at
  least $0.02 (so opening tickets cannot be repeated for free).
- Test note: tests/test_kind.py::test_concurrent_kind_changes_start_only_one_enrichment failed once in a full run and
  passed on every rerun (timing flake, unrelated code path).

## Review of integration PR #133 (backend fixes)
- book.json writers share one re-entrant lock per file (`books.write_lock`): rechunk upgrade (stamp re-checked inside
  the lock), equation updates, textbook notes, kind change (`books.update_book`, fresh read), enrich checkpoints and
  ingest publish. `save_book` writes a per-call temp file (`book.json.<uuid>.tmp`) and `os.replace`s it; on Windows a
  replace over a file a reader has open is retried briefly (this was the cause of the test_kind flake above).
- Known transient (not fixed): rechunking an old book (chunk_rules upgrade) can shift chunk ids (`p{idx}c{n}`) when a
  paragraph gains a singing split. A reader that loaded the old book JSON before the upgrade asks for the old ids until
  it reloads the book, so a few chunks may play the wrong audio once. There is no book version / ETag the client
  could check; the upgrade runs once per book at startup, so a reload after deploy clears it.
- Enrichment CPU (attribution + whole-book rechunk) runs in a worker thread; the minor-voice pass no longer rechunks
  on its own (the segment stage rechunks once). TTS chunking uses a per-book `BookIndex` (30k paras x 3k sections:
  9.4 s -> 1.8 s, committed books byte-identical).
- Emotion markers: `emotion.strip_tags` removes every `<|...|>` (repeatedly, then stray `|`); split_marker is linear and
  capped at 2000 chars; `/api/speak` strips tags from its text. An empty textbook-notes result is now a failure (keeps
  earlier notes); `start_enrich` purges abandoned equation tickets.

## #135 (backend): non-English TTS and two more sound effects

- `/api/speak` takes an optional `language` (ISO 639-3, validated against `app/tts_lang.py`, the 102 documented languages;
  the docs give names only, so the codes are ours: all 102 plus `cmn` were accepted live by Boson, 2026-10-23). English
  (absent or `eng`) is unchanged. Any other language is requested with `timestamps: false`, `language` and `normalize: true`
  (timestamps switch normalization off; they exist only for English, Chinese and Spanish, and a live Spanish clip with
  timestamps read "1888" unnormalized). The reply is then raw audio, so `timestamps` is null and the caption highlights the
  whole sentence. `cache_key` gains language and mode for non-English only, so English keys do not change.
- delivery.py already had laughter, sigh, crying, sniff, cough (Ahem/Cough) and screaming (#73). Added `sneeze`
  (Achoo, Ah-choo, Atishoo) and `humming` (Hmm-hmm / Mmm, only when the speech tag says hummed/humming). Never in
  narration or sung chunks; one sfx per chunk. `burping` is deliberately not used. CHUNK_RULES_VERSION is 2.
  Effect on the committed public-domain books and Alice: 0 chunks change (rules are strict; there is no Achoo or hummed
  line in them), so the bump only upgrades uploads and keeps books stamped.
- `<|reset|>`: the docs say only that a new group of delivery tokens replaces the old one and that reset returns to
  neutral; sfx are not delivery tokens and chunks are synthesized independently, so no reset is emitted. A live clip with
  style + sfx + `<|reset|>` was accepted and no tag was read aloud (tag words absent from word timestamps).
## Realtime hands-free on Boson turn detection, voice-timed captions, fast "didn't catch that" (#135, 2026-10-23)
- Live probes (about $0.005 in total: ~1.5 min of input audio, one short reply), `higgs-realtime`, the 2 s TTS clip
  "What is the capital of France?" streamed in real time:
  - `turn_detection` **cannot change mid-session**: `session.update` after the start -> 400 `turn_detection cannot be
    changed after session start`. So hands-free on/off reopens the socket (seeded history keeps the chat).
    `transcription.language` *can* change mid-session (`session.updated` echoes it).
  - `semantic_vad` + `create_response: false` + `interrupt_response: false` is echoed and honoured. Sequence:
    `speech_started` (item_id, audio_start_ms) -> `speech_stopped` -> `committed` (~0.05-0.1 s later) ->
    `transcription.completed` (0.15-0.4 s after the commit) -> nothing until our `response.create`. A
    `speech_started` while our response was active did not cancel it.
  - `noise_reduction: near_field` under server VAD started the turn ~1 s late and transcribed only "France." (also
    split the question into fragments transcribed as "嗯。" at threshold 0.5). Without it: start at 436 ms, one turn,
    "What is the capital of France?". Server-turn sessions therefore send no noise_reduction (the capture still has
    the browser's noise suppression and echo cancellation); push-to-talk keeps near_field.
  - Semantic's default `silence_duration_ms` 100 and threshold 0.85 cut the start; shipped: threshold 0.6, silence 500.
  - A wrong transcription hint (`language: "es"` on English speech) gave **no transcript event at all**.
  - `response.output_audio_transcript.delta` is followed by `.length` for the same fragment, `length_ms` per fragment
    (not cumulative): "Hello!" 840 ms, " How can I assist you today?" 2680 ms (= the reply's 3.52 s). Fragments are
    about a sentence, so captions interpolate words inside a fragment.
- Hands-free (TalkController `serverTurns`, on in useTalk unless `?gate=local` or localStorage `talkback:gate=local`):
  the session opens at once with semantic_vad (fallback chain: server_vad if refused, then manual turns + the local
  gate, `turn-mode` event); the mic streams continuously (up to 3 s held while connecting). speech_started starts the
  turn (reader paused, no extra mic start); speech_stopped -> transcribing; the transcript runs the normal flow
  (`/api/retrieve` instructions, spoiler-safe) and we send `response.create`. Speech restarting within 600 ms of a stop
  continues the same question (transcripts joined; the server can split one question). An answer the server starts by
  itself is cancelled and never played (and would mean the docs' "no effect" became true again).
- Echo guard: a speech_started while the character speaks (or within the 300 ms tail) counts only with the strict
  local gate (`HANDS_FREE_OVER_SPEECH`, 0.10 RMS for 400 ms) speaking; otherwise it is on probation until the level
  confirms it, and if it never does, its committed item is deleted (`conversation.item.delete`) and its transcript
  ignored. Headphones advice stays. If the session closed (5 min idle), the idle gate reopens it on speech; the held
  audio goes first.
- Captions: TalkClient builds [text, start, end] from `.length` and reveals by the player's played position
  (`RingPlayer.playedMs`, an AudioContext-clock estimate; it stops while paused); the full caption lands when playback
  drains. Without a playback clock: as before (as it arrives). On a cut (`interrupt`) the `heard` event carries the
  words up to the flushed position, and `chatLog.cutHeard` trims the interrupted (or stopped) reply to them, so the
  bubble and `priorTurns` memory match what was said.
- Fast failure: a `committed` with no transcript within 1.5 s (`FAST_FAIL_MS`), or an empty transcript, ends the turn
  with "I didn't catch that" (push-to-talk too; the 6 s window stays only when no committed event comes). A failure
  while a language hint is set clears the hint.
- Language: `language.clearLanguage` (a script, or >= 2 stopwords of one language and more than any other); the newest
  clear one of the last three questions sets `transcription.language` (ISO 639-1; English -> none). Gemini-path
  answers pass it to `/api/speak` as ISO 639-3 (`spa`, `fra`, ...; only when not English); the backend then returns
  `timestamps: null`, which the answer path never needed (sentence-level bubbles and playback).
- Not verified live: the echo guard with real laptop speakers (needs a person), how fine `.length` fragments are for
  long answers, `semantic_vad` on real (not TTS) speech with pauses.

## Hands-free echo, "didn't catch that", stacking answers, barge-in (fix/handsfree-echo, 2026-10-23)
User reports on #135 server-turn hands-free: the character heard itself, "I didn't catch that" came often, answers
stacked in the chat, interrupting it was unreliable. Root causes (code reading + fake-socket repros, no live probe):
- The mic streamed to Boson all the time, also while the character played. The echo guard only gated barge-in, so the
  server VAD still committed turns of the character's own voice: a speech_started arriving after the 300 ms echo tail
  (VAD + network delay) became a real turn, transcribed as the character's own words (a question bubble with them, and
  an answer to itself), and a loud echo that passed the strict gate cut the character off.
- Every committed server turn (clicks, breaths, noise, echo) that had no transcript after 1.5 s showed "I didn't catch
  that"; 1.5 s was also shorter than the transcription of longer real turns.
- A barge-in after our response.create but before its response.created: the cancel was "not active", the late
  response.created reset the drop flag, so the cut-off answer played and its captions went into the bubbles; output was
  never checked against its response id. The interrupted bubble also stayed pending (late text could land in it), chat
  writes went to "the last pending bubble", and an empty interrupted bubble was left behind.
- One clearly Spanish question set the transcription hint (a wrong hint gives no transcript at all).
Fixes:
- Mic hold (TalkClient.setMicHold, driven by TalkController.updateMicHold): while the character speaks and
  MIC_HOLD_TAIL_MS (600) after, no mic audio goes to the server; the held audio is dropped, except on a barge-in.
- Barge-in in server mode comes from the local strict gate (HANDS_FREE_OVER_SPEECH, now 250 ms) with a threshold
  relative to the echo: max(0.10, 2.5 x median mic level over the last 2 s of playback; createEchoFloor; the first
  300 ms always count, later only readings under the bar, so the reader's speech never raises it). On it: cut now
  (flush + cancel/truncate), release the hold with ~300 ms pre-roll, the server's speech_started continues the turn; if
  the server never reports speech within BARGE_CONFIRM_MS (2.5 s) the turn ends quietly. The local gate mode uses the
  same relative threshold.
- Turns: a server speech_started while held is echo (item deleted on commit). A turn with < MIN_TURN_MS (400 ms) of
  speech by the local level and no transcript is dropped silently and its items deleted. A real turn without a
  transcript waits failWaitMs = 2.5 s + turn/4 (max 5 s) before "I didn't catch that", and its items are deleted.
  EMPTY_TURNS_FALLBACK (2) real empty turns in a row: hands-free switches to the local gate for this session, with
  the note "I didn't catch that. Hands-free now uses the local voice gate."
- Response binding: TalkClient tracks the current response id; a cancel before response.created cancels that response
  when it shows up; any output, caption or response.done of another id is ignored.
- Chat: useTalk writes captions/sentences only to the answer bubble its question created (setTextOf/appendSentenceTo
  by id); markInterrupted closes the bubble (an empty one is removed); cutHeard only trims the newest reply; no empty
  question bubbles.
- Language: the transcription hint needs the two newest clear questions (of the last three) in the same non-English
  language; a failed real turn still clears it. The TTS answer language still follows the newest clear question.
- Not verified live (needs a person with speakers): the echo-relative threshold on real laptop echo, the 600 ms tail
  for rooms with long reverb, and whether Boson's VAD minds the gaps in the audio while held.

## Web search on Talk answers (Grounding with Google Search), branch feat/web-search-answers
- Rule (server-side, `app/websearch.allowed`): web search only when `book.kind == "textbook"` or `spoilers` is on, and
  `WEB_SEARCH` is true (default). A novel read spoiler-safe never gets the tool (the web would leak later events):
  enforced in `/api/answer` and in the public `/api/public/answer` (a client can only opt out, `web_search: false`).
- API (google-genai 2.31, Vertex docs): `GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())])`.
  gemini-2.5-flash is supported. Sources are `candidate.grounding_metadata.grounding_chunks[].web.{uri,title,domain}`;
  on Vertex the uri is a `vertexaisearch.cloud.google.com/grounding-api-redirect/...` link and the title is the domain.
  Limits: 1M grounded prompts/day; search tools cannot be combined with function calling in one request.
- Pricing (cloud.google.com/vertex-ai/generative-ai/pricing, read 2026-10-23): Gemini 2.0 Flash, 2.5 Flash and 2.5
  Flash-Lite share 1,500 free Grounding Prompts per day; past that $35 per 1,000 grounded prompts (one charge per prompt
  however many queries), billed only when the prompt returns at least one web source. (Gemini 3: 5,000 free queries
  per month, then $14 per 1,000 queries, per query.) Search-result input tokens are not charged. gemini-2.5-flash
  tokens: $0.30 / 1M in, $2.50 / 1M out.
- Display terms (Vertex "Grounding with Google Search" docs): show the Search Suggestions
  (`search_entry_point.rendered_content`, Google's HTML/CSS) exactly as provided, visible whenever the grounded answer
  is, the same width as the answer; tapping a chip goes straight to the Google results page (in-app or separate
  browser), nothing between; no other search results next to it. Its CSS handles light/dark itself
  (`prefers-color-scheme`); the docs say nothing about scripts, iframes or new tabs. Done: the backend passes the markup
  unmodified (string <= 32 KB, `websearch.clean_suggestions`) as `search_suggestions` on the `sources` line (local
  route, public route, and the clone re-checks what the service sends); the reply shows it in
  `<iframe class="search-suggestions" srcdoc sandbox="allow-popups allow-popups-to-escape-sandbox"
  referrerpolicy="no-referrer">` (no scripts, opaque origin), full bubble width, fixed 64 px height (a sandbox without
  scripts cannot measure itself; Google's chips are one scrollable row). The srcdoc wraps the markup in a document with
  `<base target="_blank">` (chips open google.com in a new tab: a framed google.com page refuses to load) and
  `color-scheme: light dark`; the markup itself is not changed. Plain source links stay above it. Not checked against a
  live rendered_content in the browser (no paid call): if Google's markup is taller than one row it is clipped/scrolls
  inside the frame; adjust `SUGGESTIONS_HEIGHT` after a manual look.
- Prompt: `personas.WEB_SEARCH_RULE`, appended after the length cap and before the spoken-output + emotion rules (the
  emotion rule stays last, the language rule stays first). Book passages stay the main source; web for background,
  definitions, author/history, (textbooks) current facts and worked examples; say "outside the book" briefly; keep the
  voice; at most four short sentences; no URLs spoken.
- Stream: `GeminiAnswerer.stream_sentences(..., web_search=True, on_sources=cb)` collects grounding chunks, cleans them
  (`websearch.clean_sources`: http(s) only, dedup, <= 5) and reports them after the last sentence; the route sends one
  NDJSON `{"type":"sources","sources":[{uri,title}]}` line before `done`. If the grounded request is refused at start
  it retries once without the tool. Usage log: `usage.record("llm", ..., grounded=True, sources=n)`.
- Caps: local `GROUNDED_DAILY_CAP` (default 100/UTC day, `data/usage/grounded_answers.json`), past it the plain answer.
  Public service: `PUBLIC_GROUNDED_DAILY_CAP` (default 50, `public_grounded_answers.json`) and each grounded answer
  reserves an extra $0.035 (`GROUNDED_USD`, worst case past the free tier) on the existing daily answer budget, settled
  to that fee only when sources came back; no room -> plain answer. Generic `DailyCount` moved to `app/daily_count.py`.
- Frontend: `api.streamAnswer(..., onSources)`, `chatLog.setSources` + `ChatMessage.sources` (re-cleaned on load from
  sessionStorage, never in the history sent back), `reader/SourceLinks.tsx` under character bubbles. useTalk.ts change
  is one line (the extra callback) to keep merges with the TalkClient/handsFree work easy.
- Realtime path (ANSWER_BACKEND=realtime / Boson): unchanged. Boson's own web_search tool was not added; voice answers
  through Realtime never search the web.
- Live check (3 real calls, ~$0.0025 of model tokens, grounding inside the free daily tier): a textbook question
  (tf-idf vs BM25) came back grounded with 5 sources (sigmacomputing, wikipedia, ibm, myscale, medium) but ran to 12
  sentences, so the rule now caps answers at four sentences. A spoilers-on Alice question ("who inspired you, when was
  it published") had the tool but the model answered from memory both times (0 sources, no "outside the book" phrase):
  Gemini decides when to search; well-known facts often skip it. Not re-checked after the four-sentence rule (call
  budget). Also seen: the sentence splitter breaks after "Mr." (pre-existing, `_split`).
