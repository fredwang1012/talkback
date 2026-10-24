# Bookson notes

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
