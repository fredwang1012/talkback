# Bookson — Claude Code implementation brief

> Paste this whole file into Claude Code (or save it as `docs/BRIEF.md` in an empty repo and tell Claude Code: "Read docs/BRIEF.md and execute it milestone by milestone").
> API facts below were checked against docs.boson.ai and the official Boson tutorial repo on 2026-10-15. Where docs and live behaviour may differ, Milestone 0 tells you to probe it.

---

## 0. How you (Claude Code) should work

1. Start in **plan mode**. Read this entire brief, then read the Boson docs index at `https://docs.boson.ai/llms.txt` and every page linked under "Higgs Realtime", "Higgs TTS 3", "Higgs Avatar", and the API reference pages for `/v1/audio/speech`, `/v1/audio/voices`, `/v1/realtime/client_secrets`, client events, server events, and errors. Append `.md` to any docs URL to get clean markdown.
2. Clone the official tutorial for reference: `git clone https://github.com/boson-ai/higgs-realtime-tutorial /tmp/hrt` and read `docs/part-0.md` through `docs/part-5.md`. It is Apache-2.0. We will **reuse its audio and realtime client code** (with the license notice kept in copied files). Its gotchas are real and were verified against live sessions.
3. For anything involving the Anthropic SDK (preprocessing scripts), use the bundled `claude-api` skill / the official docs at `https://platform.claude.com/docs` rather than guessing parameter names.
4. Work **milestone by milestone** (Section 9). After each milestone: run the acceptance check, run tests, `git commit`, and append findings to `NOTES.md`. Do not start the next milestone until the current one passes.
5. Ask me (Frederick) only when blocked on: API keys, a product decision not covered here, or before any single step that would spend more than **$5** of Boson credit.
6. Never put `BOSON_API_KEY` or `ANTHROPIC_API_KEY` in frontend code, logs, or git. `.env` is gitignored from the first commit.

---

## 1. What we are building

**Bookson** ("Audible++"): a web app that reads books and textbooks aloud and lets you **talk back**. Built for a 24-hour Boson AI hackathon, so the demo loop must be flawless before anything else is added.

Two modes, one toggle:

- **Read mode**: reads the text verbatim with expressive TTS, synchronized word highlighting, and optional **full cast** (each character speaks in their own voice; narrator in another).
- **Talk mode (Q&A)**: hold a key, ask a question by voice, get a spoken answer grounded in the text (RAG), then reading **resumes from the same sentence**. You can talk to:
  - a **character** from the book (roleplay), or
  - a **narrator/tutor** that explains concepts (for textbooks).

Differentiators (these are what we pitch, so they must work):

1. **Interrupt → answer → resume** feels seamless (the core loop).
2. **Spoiler-safe characters**: retrieval and context are limited to text the reader has already reached. A character cannot reveal anything after the reader's position.
3. **Full-cast reading** with per-character voices and emotion tags.
4. **Multilingual Q&A**: ask in any language; the answer comes back in that language, grounded in the English text.
5. Optional: **custom voices** (voice cloning from a short clip with an explicit consent checkbox) and **avatars** (talking-head video in read mode).

Demo content (both free to use):
- Novel: *The Adventures of Sherlock Holmes*, story 1 "A Scandal in Bohemia" (Project Gutenberg #1661, public domain, plain text at `https://www.gutenberg.org/cache/epub/1661/pg1661.txt`). Watson is the first-person narrator, which makes the narrator a character too.
- Textbook: OpenStax *Biology 2e*, Chapter 8 "Photosynthesis" (CC BY 4.0, attribution required in UI). Try fetching from openstax.org; if the page is JS-rendered or blocked, stop and ask me to paste the text into `data/raw/biology2e_ch8.txt`.

The ingestion pipeline must be generic (any `.txt` I drop into `data/raw/` with a small YAML metadata file), but the demo uses the two above.

---

## 2. Architecture

```
frontend (React + Vite + TypeScript + Tailwind)
  ├─ Reader (read mode): fetches TTS chunks from backend, plays them, highlights words
  ├─ RealtimeClient (talk mode): browser ⇄ wss://api.boson.ai/v1/realtime directly,
  │     authenticated with a per-connection ephemeral key minted by our backend
  └─ calls backend for: books, TTS chunks, retrieval, ephemeral keys, voice cloning

backend (Python 3.11+, FastAPI, uv)
  ├─ POST /api/realtime/token     → mints ephemeral key (never cached, one per connection)
  ├─ GET  /api/books, /api/books/{id}
  ├─ POST /api/tts                → Boson TTS proxy with disk cache (+ word timestamps)
  ├─ POST /api/retrieve           → spoiler-filtered hybrid retrieval + story-so-far + persona
  ├─ POST /api/voices/clone       → registers a custom voice (consent required)
  └─ GET  /api/avatar/{chunk_id}  → pre-rendered avatar MP4 if present (stretch)

preprocess/ (Python scripts, run offline, use the Anthropic API — NOT used at runtime)
  ingest → segment (speaker + emotion) → cast & personas → story-so-far summaries
  → retrieval chunks + context blurbs → embeddings/BM25 index → pre-render TTS for demo
```

Why this shape:
- The browser connects **directly** to Boson Realtime with an ephemeral key, so our backend never holds a long-lived WebSocket. This is the pattern in Boson's docs and tutorial, and it makes deployment trivial.
- Claude is used **only offline** for preprocessing (speaker attribution, personas, summaries, eval). At runtime the app runs entirely on Boson APIs plus our own retrieval. This keeps the demo fast and keeps the hackathon story "built on Boson".

Repo layout:

```
bookson/
  CLAUDE.md                 # short project conventions for future sessions
  NOTES.md                  # API probe results, decisions, gotchas found
  .env.example              # BOSON_API_KEY=, ANTHROPIC_API_KEY=, BOSON_BASE_URL=https://api.boson.ai
  data/
    raw/                    # source .txt + <name>.meta.yaml
    books/<book_id>/        # book.json, index/ (embeddings.npy, bm25.pkl, chunks.json)
    cache/tts/              # <sha>.mp3 + <sha>.json
    cache/avatar/           # <chunk_id>.mp4 (stretch)
    portraits/              # character/narrator portrait images (original art only)
  preprocess/               # scripts listed in Section 5
  backend/app/              # main.py, config.py, boson.py, tts_cache.py, retrieval.py, books.py, personas.py
  backend/tests/
  frontend/                 # Vite app; src/audio + src/realtime adapted from the tutorial
```

---

## 3. Boson API facts you must respect (verified 2026-10-15)

### 3.1 Auth and billing
- REST: `Authorization: Bearer $BOSON_API_KEY`. Base `https://api.boson.ai`.
- 429 with `error.type: "rate_limit_error"` is transient → retry with exponential backoff + jitter. 429 with `error.type: "insufficient_quota"` is **not** transient → never retry; surface a clear error. Always branch on `error.type`.
- Realtime close codes: `3000` invalid/expired key, `4429` quota refusal (preceded by an `error` event), `1013` concurrency limit (retry later).

### 3.2 Higgs TTS 3 — `POST /v1/audio/speech`
- Body: `{ model: "higgs-tts-3", input, voice?, response_format?, stream?, ref_audio?, ref_text?, enable_tn?, tn_language?, timestamps? }`.
- **Input length: keep each request ≤ ~300 characters.** Longer input can come back truncated or garbled **with a 200 status** (no error). Hard validation limit is 5000 chars. Our chunker targets ≤ 280 chars, split on sentence boundaries.
- `voice` = preset name or custom `voice_<id>`. Mutually exclusive with `ref_audio`/`ref_text`. **Always send the same voice for a given speaker on every chunk** so the voice never drifts.
- Preset voices documented: `chloe`, `eleanor`, `jake`, `marcus`, `nora`, `oliver`, plus `default`. (`berlinda` appears in one doc example but not the table: verify in M0 before using.)
- `timestamps: true` → response becomes JSON `{ audio: <base64 in response_format>, response_format, input, timestamps: [{word,start,end}] | null }`. Requires `stream: false`. Word timestamps only for **English, Chinese, Spanish**; others return `null`. Chinese returns one entry per character. **Requesting timestamps disables text normalization** (`enable_tn` is overridden), so numbers/abbreviations are spoken as written: do light normalization ourselves for the TTS text (e.g. "1888" → "eighteen eighty-eight", "Mr." is fine).
- `stream: true` requires `response_format: "pcm"` (raw 24 kHz 16-bit mono). We do **not** stream for read mode (we need timestamps and we prefetch anyway).
- Inline tags (put delivery tags at the **start** of the input): `<|emotion:X|>` where X ∈ elation, amusement, enthusiasm, determination, pride, contentment, affection, relief, contemplation, confusion, surprise, awe, longing, anger, fear, disgust, bitterness, sadness, shame, helplessness (do not use `arousal`); `<|style:whispering|>`, `<|style:shouting|>`; `<|prosody:speed_slow|speed_fast|speed_very_slow|speed_very_fast|pitch_low|pitch_high|expressive_high|expressive_low|>` at the start; positional `<|prosody:pause|>` / `<|prosody:long_pause|>` where the break goes; `<|sfx:laughter|>Haha` style sfx must be followed by onomatopoeia. We use at most one emotion + optional style/prosody per chunk.
- Pricing: $0.015 per 1K input characters; generated audio is free. A ~45K-char story ≈ $0.70.

### 3.3 Custom voices — `POST /v1/audio/voices`
- Body (schema): `{ ref_audio: URL|data URI|base64 (min 3.0 s, max 10 MB), ref_text: string (required), description?: string }`. The schema sets `additionalProperties: false`, so send `description`, **not** `title` (one guide example uses `title`; follow the schema, and log the actual error if it rejects).
- Response field is `voice` (schema) but the guide shows `voice_id`: **accept either**.
- Same audio under the same key returns the same id (idempotent). Recommended clip: 5–30 s clean speech, verbatim transcript including filler words.
- Docs require that the user owns the right to clone the voice → UI requires a consent checkbox; backend rejects requests without `consent: true`.
- The same `voice_<id>` works in Realtime (`audio.output.voice`).

### 3.4 Higgs Realtime — `wss://api.boson.ai/v1/realtime`
- Browser auth: backend calls `POST /v1/realtime/client_secrets` with body `{"expires_after":{"seconds":600}}` → returns `{ value: "bai-eph-…", expires_at }`. Browser opens `new WebSocket(url, ["realtime", "bai-client-secret." + value])`. **Mint a fresh key per connection; never cache.**
- Session config via `session.update` (first one is acked with `session.created`; nothing arrives before you send it; don't wait for `session.created` first). Fields we use: `model: "higgs-realtime"`, `instructions`, `output_modalities: ["audio"]` (exactly one modality), `audio.input.format {type:"audio/pcm", rate:24000}`, `audio.output.format` same, `audio.output.voice`, `audio.input.turn_detection`, `audio.input.transcription {model:"higgs-stt-3.1"}`, `audio.input.noise_reduction {type:"near_field"}`, `temperature` (default 0.3).
- **Unknown voice fails `session.update`** (validated at update time).
- `conversation.item.create` supports **text only**: user `input_text`, assistant `text`. `system`-role items are rejected. `function_call_output` works.
- `response.create` accepts per-response overrides: `response.instructions` (system prompt for this response), `response.input` (message items, also appended to conversation), `temperature`, `max_output_tokens`, `metadata`.
- With `turn_detection: null` (manual turns): you `input_audio_buffer.append` (≤ 1 MiB base64 per event, send small chunks), then `input_audio_buffer.commit`, then `response.create`. Sending `response.create` while a response is active is rejected with `voice_output_task_ongoing` → send `response.cancel` first and wait for `response.done (status: cancelled)`.
- Input transcription arrives **only** as `conversation.item.input_audio_transcription.completed` (no deltas).
- Output: `response.output_audio.delta` (base64 PCM16 24 kHz mono), `response.output_audio_transcript.delta/done`, `response.done`. `response.done.usage` is always null.
- Barge-in bookkeeping: when the user interrupts playback, flush local playback and send `conversation.item.truncate { item_id, content_index: 0, audio_end_ms }` where `audio_end_ms` = samples actually handed to the audio output ÷ 24 (the tutorial's worklet counter). The server generates audio much faster than real time, so "audio received" is the wrong number.
- Session limits: **idle timeout after 5 minutes without detected user speech** (`session.idle_timeout`, then close). Max wall-clock duration (`session.max_duration_reached`). Our app must treat a closed session as normal and **reconnect transparently on the next push-to-talk**.
- The model detects the spoken language and replies in kind.
- Tolerate unknown event types (`conversation.context.summarized`, `response.output_audio_transcript.length`, etc.). Never crash on them.
- **React StrictMode opens two sockets and nothing complains** (two live sessions, double audio, double bill). Guard `connect()` with a connecting flag and make it idempotent.
- Tool calls may be announced twice (in `function_call_arguments.done` and in `response.done.output`): dedupe by `call_id`.
- Prompting lesson from the tutorial: long system prompts made the model stop calling tools. **Keep instructions short and behavioural.** Put grounding text in a clearly delimited block, not as more rules.
- Pricing (approx.): audio in ~$0.0023/min, audio out ~$0.014/min, transcription $0.0025/min.

### 3.5 Higgs Avatar — `POST /v1/videos` (async) and `POST /v1/videos/stream` (fMP4)
- Body: `{ model: "higgs-avatar", ref_image (PNG/JPEG/WebP; URL, data URI, base64, or multipart), size: "640x640"|"640x480"|"480x640", and exactly one of input (audio ≤ 60 s) or input_tts {model:"higgs-tts-3", input, voice} }`.
- Async: poll `GET /v1/videos/{id}` until `completed|failed`; download `GET /v1/videos/{id}/content` (404 until done). Streaming returns fMP4 with id in `X-Video-Id` header (needs Media Source Extensions in browser).
- It does **not** hold a conversation. Pricing ~$0.07–0.09/min.
- Plan: pre-render avatar clips **offline for the demo chapter only**, driven by the same cached TTS audio. Verify in M9 whether the MP4 contains an audio track; if yes, play the MP4 instead of the MP3 for that chunk; if not, play both in sync.

---

## 4. Data model (`data/books/<book_id>/book.json`)

```jsonc
{
  "id": "scandal-in-bohemia",
  "title": "A Scandal in Bohemia",
  "author": "Arthur Conan Doyle",
  "kind": "novel",                 // "novel" | "textbook"
  "license": "Public domain (Project Gutenberg #1661)",
  "attribution": null,             // required string for CC BY content
  "sections": [{ "id": "s1", "title": "I.", "start_para": 0 }],
  "paragraphs": [
    {
      "idx": 0,
      "section_id": "s1",
      "text": "To Sherlock Holmes she is always the woman. ...",   // verbatim source text
      "segments": [                  // verbatim partition of `text` by speaker
        { "speaker": "narrator", "text": "...", "emotion": "contemplation" }
      ],
      "tts_chunks": [                // ≤ 280 chars each, sentence-aligned, within one segment
        {
          "id": "p0c0",
          "speaker": "narrator",
          "voice": "marcus",
          "tts_text": "<|emotion:contemplation|>To Sherlock Holmes she is always the woman.",
          "char_start": 0, "char_end": 43   // offsets into paragraph.text for highlighting
        }
      ]
    }
  ],
  "characters": [
    {
      "id": "holmes", "name": "Sherlock Holmes", "aliases": ["Holmes"],
      "first_para": 3,
      "voice": "oliver", "voice_tags": "<|prosody:pitch_low|>",
      "persona": "≤ 3 sentences: personality, speaking style, verbal tics",
      "portrait": "portraits/holmes.png"
    }
  ],
  "narrator": { "id": "narrator", "voice": "marcus", "is_character": "watson" },
  "summaries": [ { "upto_para": 19, "text": "Story so far, written using only paragraphs 0..19" } ]
}
```

Retrieval index (`index/chunks.json`): retrieval chunks of ~2–4 paragraphs with 1-paragraph overlap, each with `{ id, para_start, para_end, text, context_blurb }`. Embeddings are computed over `context_blurb + "\n" + text`.

**Spoiler rule (hard invariant):** a retrieval chunk is eligible only if `para_end <= reader_para`. Summaries used must have `upto_para <= reader_para` (pick the largest). Characters whose `first_para > reader_para` are hidden from the Talk picker. Enforce this in the backend, never trust the client or the model for it, and unit-test it.

---

## 5. Preprocessing pipeline (`preprocess/`, Anthropic API, offline)

Use the Anthropic Python SDK with `ANTHROPIC_API_KEY`. Models: `claude-haiku-5-5` for bulk per-paragraph work, `claude-sonnet-5-5` for whole-story reasoning (cast, personas, summaries, eval generation), `claude-opus-5-5` only as the eval judge. Use **structured outputs** (`output_config.format` with a JSON schema) for every call that returns data.

Haiku 5.5 specifics (from its migration guide): do **not** send `temperature`, `top_p`, or `top_k` (errors); no assistant prefill (end `messages` with a user turn); adaptive thinking may emit `thinking` blocks, so select content blocks by `type`, never by index; set a low effort for bulk calls; handle `stop_reason: "refusal"` gracefully (fallback: narrator-only for that paragraph). Run calls concurrently with an asyncio semaphore (8) and retries. Cache every LLM result to `data/cache/llm/<sha>.json` so reruns are free. Print a cost estimate before and actual token usage after each script.

Scripts (each idempotent, each with `--book <id>`):

1. `ingest.py`: read `data/raw/<name>.txt` + `<name>.meta.yaml` (title, author, kind, license, attribution, start/end markers). Strip Gutenberg header/footer, normalize whitespace and curly quotes, split sections and paragraphs. For the Sherlock demo, extract only "A Scandal in Bohemia".
2. `cast.py` (Sonnet): read the whole text; output characters with aliases, `first_para`, a ≤ 3-sentence persona, and a voice assignment from the available presets (narrator gets its own voice; differentiate same-gender characters with `voice_tags` like `pitch_low`/`pitch_high`). Write to `book.json`. I will review and edit the casting by hand.
3. `segment.py` (Haiku): for each paragraph, given the cast list and the previous 3 paragraphs as context, output `segments` (speaker id or `narrator`, verbatim text, one emotion from the allowed list or null). **Validate:** concatenating segment texts must equal the paragraph text after whitespace normalization. If not, retry once, then fall back to a single narrator segment. Speech tags ("said Holmes") belong to the narrator.
4. `chunk_tts.py` (no LLM): split each segment into TTS chunks ≤ 280 chars on sentence boundaries (handle "Mr.", "Mrs.", "Dr.", "St.", initials, ellipses, quotes); if one sentence exceeds 280, split at the last comma/semicolon. Build `tts_text` = voice tags + emotion tag + lightly normalized text. Record `char_start/char_end`. Unit tests: every chunk ≤ 280 chars of spoken text, verbatim coverage of the paragraph, no empty chunks.
5. `summaries.py` (Sonnet): rolling "story so far" summaries every ~10 paragraphs and at every section end, each written **only from text up to `upto_para`** (pass only that prefix, never the whole book). ≤ 120 words each.
6. `index.py`: build retrieval chunks; generate a one-sentence `context_blurb` per chunk with Haiku (situating it in the story, using only preceding text); embed with `intfloat/multilingual-e5-small` via sentence-transformers (prefix `passage: ` for chunks and `query: ` for queries; normalize vectors). Also build a BM25 index (`rank_bm25`). Multilingual embeddings are what make Mandarin or Spanish questions retrieve English passages.
7. `prerender_tts.py`: call the backend TTS path for every chunk of a book (respect concurrency 4, backoff on `rate_limit_error`). Print character count and estimated cost first and ask for confirmation if > $5.
8. `eval_spoilers.py`: Sonnet generates ~100 questions at random reader positions (half answerable from earlier text, half that require later text, e.g. "How does the story end?"). For each, run the real `/api/retrieve` and the real Talk prompt through **text mode** of Higgs Realtime (`output_modalities: ["text"]`) or, if that is awkward from Python, through the same prompt on Sonnet as a proxy, and have Opus judge: leaked future info? answered earlier info correctly? Report leak rate and accuracy. Target: 0 leaks.
9. `prerender_avatar.py` (stretch, M9).

Textbook variant: `cast.py` produces no characters, only a narrator/tutor persona; `segment.py` is skipped (single narrator segment per paragraph, no emotion except occasional `awe`/`enthusiasm` on key sentences chosen by Haiku); spoiler filtering is disabled for textbooks (the tutor may use the whole chapter) but retrieval is still position-aware: rank passages near the reader's position higher.

---

## 6. Backend (`backend/app/`, FastAPI)

- `config.py`: pydantic-settings; loads `.env`. Fail fast with a readable error if `BOSON_API_KEY` is missing.
- `boson.py`: thin async httpx client: `create_speech(...)`, `create_voice(...)`, `mint_client_secret(ttl)`, `create_video(...)`, `get_video(...)`, `download_video(...)`. Central error handling per Section 3.1. Global semaphore for TTS (4 concurrent).
- `POST /api/realtime/token` → `{ value, expires_at }`. Fresh key each call. Rate-limit this endpoint per client IP (simple in-memory token bucket) so a public deploy cannot be abused.
- `GET /api/books` → list (id, title, author, kind, license, attribution, cover).
- `GET /api/books/{id}` → `book.json` minus internal fields (no full summaries beyond what the client needs; personas are fine).
- `POST /api/tts { chunk_id, book_id, voice_override? }` →
  - Cache key = sha256 of (model, tts_text, voice, response_format, timestamps flag).
  - Cache hit → return `{ audio_url: "/media/tts/<sha>.mp3", timestamps }`.
  - Miss → call Boson with `response_format: "mp3"`, `timestamps: true`; decode base64 audio to file; store timestamps JSON; return the same shape.
  - Map timestamps to paragraph character offsets: walk timestamp words and the chunk's plain text in order with normalized fuzzy matching (lowercase, strip punctuation, ignore tag tokens). Return `[{ start, end, char_start, char_end }]`. If timestamps are null, return null and the frontend falls back to chunk-level highlighting.
- `POST /api/retrieve { book_id, query, reader_para, speaker_id, k=4 }` →
  - hybrid score = 0.6 · cosine(e5) + 0.4 · normalized BM25, over eligible chunks only (spoiler rule), plus a small recency boost for chunks within 15 paragraphs of `reader_para`.
  - Always include the 2 paragraphs immediately before `reader_para` ("what just happened"), deduped.
  - Return `{ passages: [{para_start, para_end, text}], story_so_far, persona, instructions }` where `instructions` is the fully built Talk prompt (Section 7.4), so prompt logic lives in one place (Python) and is testable.
  - Hard cap: passages total ≤ 6,000 characters.
- `POST /api/voices/clone` (multipart: `audio`, `transcript`, `name`, `consent`) → 400 unless `consent == "true"`; validates duration ≥ 3 s and size ≤ 10 MB; calls Boson; returns `{ voice }`; stores mapping in `data/voices.json`.
- `GET /api/avatar/{chunk_id}` → file if pre-rendered, else 404.
- Static mount `/media` for cached audio/video. CORS for the Vite dev origin.
- Tests (pytest): spoiler invariant (property test over random positions: no returned passage has `para_end > reader_para`, no summary beyond position), TTS cache key stability, timestamp alignment on fixtures, token endpoint never returns the real key, clone endpoint rejects missing consent.

---

## 7. Frontend (`frontend/`, React 18 + Vite + TypeScript + Tailwind)

### 7.1 Reuse from the tutorial (`/tmp/hrt`, Apache-2.0, keep license headers)
Copy and adapt: `src/audio/capture.ts`, `src/audio/resample.ts`, `src/audio/playback.ts`, `src/audio/loadWorklet.ts`, `src/audio/worklets/*`, `src/realtime/events.ts`, `src/realtime/RealtimeClient.ts`, `src/state/transcriptStore.ts`, `src/ui/DebugDrawer.tsx`. Keep their tests. Keep mic constraints `echoCancellation: true, noiseSuppression: false, autoGainControl: true, channelCount: 1`. Keep the 24 kHz playback context so the played-samples counter maps exactly to `audio_end_ms`.

Adapt `RealtimeClient` for **manual turns** (Section 7.3), dynamic per-response instructions, transparent reconnect, and voice per character.

### 7.2 Read mode player (`src/reader/`)
- `ReaderEngine` class (framework-agnostic, unit-testable) owns: book, current chunk index, queue, state (`idle|loading|playing|paused|ended`), speed.
- Playback uses **`HTMLAudioElement` per chunk** with `preservesPitch = true` and `playbackRate = speed` (0.75–1.5) so speed changes do not shift pitch. Keep the next 3 chunks prefetched (request `/api/tts`, create `Audio`, `preload = "auto"`); start the next element on `ended` for near-gapless playback. Cancel stale prefetches on seek.
- Word highlight: `requestAnimationFrame` loop compares `audio.currentTime` to the chunk's timestamps; highlight the current word span in the paragraph; auto-scroll to keep the active paragraph in view (pause auto-scroll for 5 s if the user scrolls manually). Fallback: highlight the whole chunk.
- Controls: play/pause, previous/next sentence (chunk), click any sentence to jump there, speed, "full cast" on/off (off = narrator voice for everything), voice picker per speaker (Cast panel).
- Track `reader_para` = paragraph of the current chunk; it drives spoiler filtering and the Talk character list.
- Persist position per book in `localStorage` (fine in our own Vite app).

### 7.3 Talk loop (the core loop; build this as an explicit state machine `src/talk/TalkController.ts`)

States: `reading → listening → transcribing → retrieving → answering → resuming → reading` (plus `talk-idle` when the user is in Talk mode without reading).

Input: **push-to-talk** (hold **Space**, or hold the big mic button; touch support). Push-to-talk is the default because it removes echo from read-mode audio, avoids the 5-minute idle timeout problem, and gives us a deterministic moment to inject retrieval. Session config uses `turn_detection: null` and `transcription: { model: "higgs-stt-3.1" }`.

On **PTT down**:
1. If reading: pause the `ReaderEngine` and remember `{chunkIndex}` (resume point = start of that chunk, i.e. start of the sentence).
2. If an answer is playing: flush playback, send `conversation.item.truncate` with the played ms, send `response.cancel`.
3. Ensure the Realtime session is open (connect if closed: mint key → open socket → `session.update` with the current speaker's voice). Buffer mic audio locally while connecting, then send it.
4. Start streaming mic audio (`input_audio_buffer.append`, small chunks).
5. UI: "Listening…" with a live level meter.

On **PTT up**:
1. Stop the mic, send `input_audio_buffer.commit`. Ignore presses shorter than 300 ms (treat as a tap = toggle play/pause).
2. Wait for `conversation.item.input_audio_transcription.completed` (timeout 2,500 ms; on timeout use an empty query and rely on "what just happened" passages).
3. Call `/api/retrieve` with the transcript, `reader_para`, and selected speaker.
4. Send `response.create` with `response.instructions = retrieve.instructions`. (Do not add the passages as a conversation item; keep them out of history so the context stays small.)
5. Play the answer; show the live transcript of the answer as captions.

On `response.done` and playback drained:
- If "auto-resume" is on (default on): wait 1.5 s (a press of PTT during this window starts a follow-up question instead), then resume the reader at the saved chunk.
- Talk-mode-only (not reading): stay in `talk-idle`.

Character switching: changing the selected speaker sends `session.update` with the new `audio.output.voice`. If M0 finds that mid-session voice changes are rejected or ignored, close and reconnect with the new voice instead.

Session hygiene: on `session.idle_timeout`, `session.max_duration_reached`, or any close, mark the session closed silently; the next PTT reconnects. Surface only real errors (quota, key) in a toast.

Optional "hands-free" toggle (stretch): `semantic_vad` + a `search_story` function tool (description carries all retrieval guidance; arguments: `query` in English; the client adds `reader_para` itself, never the model). Only build after the PTT loop is perfect.

### 7.4 Talk prompt (built in backend `personas.py`, keep it short)

Character template:

```
You are {name} from "{title}", chatting with a reader who has paused the story.
Speak as {name}: {persona}
Keep replies to two to four spoken sentences. No lists, no markdown, no stage directions.
You only know what has happened up to where the reader stopped. If asked about anything later, stay in character and say you cannot know yet, without hinting.
Use the notes below as your memory. Do not invent plot facts that are not in them.
Always reply in the language the reader used.

### Story so far
{story_so_far}

### Passages the reader has already read
{passages}
```

Narrator/tutor template (textbooks and the "Narrator" option for novels):

```
You are a warm, vivid narrator explaining "{title}" to a curious listener, in the style of a nature documentary: calm, awed by the details, concrete.
Explain in plain words, with one vivid example if it helps. Two to five spoken sentences. No lists, no markdown, no formulas read symbol by symbol.
Ground every claim in the passages below; if they do not cover the question, say so briefly and offer what they do cover.
Always reply in the language the listener used.

### Passages
{passages}
```

Keep these templates in one file with unit tests that render them for fixtures. Total instructions ≤ ~8,000 characters.

### 7.5 UI
- **Library**: two cards (Sherlock, Biology). Attribution footer for OpenStax ("Biology 2e by OpenStax, CC BY 4.0", link).
- **Reader view**: left = text (sections, paragraphs, active word highlight, dimmed future text optional); right = **Stage** panel: current speaker portrait (or avatar video when available), speaker name, mode toggle **Read / Talk**, character picker (only characters already introduced; a lock icon with "not met yet" for the rest is a nice touch), big mic button, captions.
- **Bottom player bar**: play/pause, prev/next sentence, speed, full-cast toggle, auto-resume toggle, position "¶ 42 / 160".
- **Spoiler badge** on the Stage: "Holmes knows up to ¶ 42".
- **Cast panel** (drawer): per speaker voice dropdown (presets + cloned voices), "preview" button (short TTS sample), portrait.
- **Voice lab** (drawer, stretch): record or upload 5–30 s, transcript field (pre-filled with a script the user reads aloud), consent checkbox ("This is my voice, or I have permission to clone it"), Create voice.
- **Debug drawer** from the tutorial, toggled with `` ` ``.
- Keyboard: Space hold = talk, K = play/pause, ←/→ = sentence, [ / ] = speed, T = toggle mode.
- Design: dark, warm "reading lamp" theme; serif for book text (e.g. Literata), sans for UI; respects `prefers-color-scheme`. Smooth state transitions; the Stage shows a clear visual for each Talk state (listening pulse, thinking shimmer, speaking waveform from an AnalyserNode).
- Portraits: original images only (generated or drawn); place in `data/portraits/`. Use a tasteful placeholder silhouette until provided.

---

## 8. Non-functional requirements
- **Latency budget for the core loop** (PTT release → first answer audio): target < 2.0 s. Log each stage (commit→transcript, retrieve, response.create→first delta) to the Debug drawer and `NOTES.md`.
- **Resilience**: every network call has a timeout; read mode keeps playing if Talk fails; Talk failures show a toast and resume reading.
- **Cost guard**: show a running estimate of Boson spend this session in the Debug drawer (TTS chars × rate, Realtime minutes × rate). Remind me in `README.md` to set a monthly cap in the Boson workspace.
- **Demo mode**: `?demo=1` loads Sherlock at a preset position (just after Holmes reads the King's letter), forces pre-rendered audio only, and pins the character to Holmes.
- **Tests**: backend pytest, frontend vitest (ReaderEngine, TalkController with a fake RealtimeClient, chunker). CI not needed.
- **Docs**: `README.md` with setup (uv + npm), env vars, preprocessing commands in order, run commands, and a 90-second demo script.

---

## 9. Milestones (do them in order; each ends with an acceptance check)

**M0 — Setup and API probes (do first, record everything in NOTES.md)**
- Monorepo skeleton, `.env.example`, `.gitignore`, `CLAUDE.md`, uv backend, Vite frontend, `npm run dev` + `uv run uvicorn` both start.
- `scripts/probe_*.py` that verify against the live API: (a) TTS with a preset voice, with tags, with `timestamps: true` (print the timestamp words, and check whether tag tokens appear in them); (b) list which preset names work (`chloe, eleanor, jake, marcus, nora, oliver, berlinda, default`); (c) mint a client secret; (d) Realtime manual-turn flow from Python: send ~3 s of synthesized speech (generate it with TTS, convert to 24 kHz PCM), commit, confirm `input_audio_transcription.completed` arrives and measure its latency, then `response.create` with custom `response.instructions` and confirm the answer follows them; (e) mid-session `session.update` changing `audio.output.voice`: accepted? audible? (f) create-voice body with `description` works and which response field holds the id.
- Acceptance: all probes pass or their failure modes are documented with a chosen workaround.

**M1 — Preprocess Sherlock** (`ingest`, `cast`, `segment`, `chunk_tts`, `summaries`, `index`). Acceptance: `book.json` validates against a pydantic schema; segment verbatim check passes for ≥ 95% of paragraphs (rest fall back to narrator); I review the cast file.

**M2 — Backend core** (`/api/books`, `/api/tts` + cache + timestamp alignment, `/api/realtime/token`, `/api/retrieve` + spoiler invariant tests). Acceptance: pytest green; curl a chunk twice and the second call is a cache hit.

**M3 — Read mode UI** (ReaderEngine, highlight, controls, prefetch, speed, full-cast toggle). Acceptance: reads the first 20 paragraphs with no audible gaps > 150 ms, highlight tracks words, seeking works.

**M4 — The core loop** (TalkController, PTT, manual turns, retrieval injection, answer playback, auto-resume, interruption of an answer, transparent reconnect). Acceptance: 10 consecutive interrupt → ask → answer → resume cycles without a stuck state; latency logged; answers cite story facts correctly.

**M5 — Spoiler safety** (character gating in UI, `eval_spoilers.py`). Acceptance: 0 leaks on the eval set; report saved to `NOTES.md`.

**M6 — Textbook mode** (ingest OpenStax ch. 8, narrator/tutor persona, attribution). Acceptance: reading and Q&A work on the textbook; tutor answers are grounded.

**M7 — Multilingual** (verify Mandarin, Spanish, French questions retrieve the right passages via e5 and get answers in the same language; add a small language indicator from the transcript). Acceptance: 5 scripted multilingual questions answered correctly.

**M8 — Voice lab** (cloning with consent; cloned voices selectable in Cast panel and usable in Talk). Acceptance: create a voice from my own recording and hear it in both modes.

**M9 — Avatars (stretch)** (`prerender_avatar.py` for the demo section, Stage plays the MP4 synced to read mode; static animated portrait in Talk). Acceptance: demo section plays with a lip-synced avatar.

**M10 — Polish and demo** (demo mode, loading states, error toasts, README, optional deploy: frontend on Vercel, backend on Railway with persistent volume for `data/cache`). Acceptance: the 90-second demo runs end to end three times in a row from a cold start.

---

## 10. The 90-second demo script (build toward this)
1. Open Bookson → Sherlock (demo mode). Full-cast reading starts: Watson narrates, Holmes and the King each in their own voice, words highlighting.
2. Hold Space mid-scene: "Holmes, what do you make of this visitor?" → Holmes answers in character from what we've read; reading resumes from the same sentence.
3. Hold Space and ask in Mandarin: "你觉得这封信是谁写的?" → Holmes answers in Mandarin.
4. Ask "How does this end?" → Holmes refuses in character ("Data, my dear reader, I have not yet got it"). Point at the spoiler badge.
5. Switch to Biology → narrator explains photosynthesis; ask a follow-up; it answers grounded in the chapter.
6. (Stretch) Cast panel → swap the narrator to my own cloned voice → it keeps reading in my voice.

Start now with plan mode and Milestone 0.
