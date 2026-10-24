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
