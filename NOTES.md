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
