# Bookson notes

## 2026-10-15 — M0 scaffold
- Python pinned to 3.12 for the backend (system Python is 3.14; torch/sentence-transformers wheels may lag). uv 0.13.1 installed via pip.
- Frontend scaffold is React 19 (Vite template default), not 18 as the brief says; tutorial code typechecks and its 31 tests pass.
- `frontend/src/tools` (tutorial's dummy search) kept only so RealtimeClient compiles; remove in M4 when RealtimeClient is rewritten for manual turns.
- Docs findings beyond the brief: timestamps may be `null` or `[]`; `input_audio_buffer.commit` also errors with `voice_output_task_ongoing` mid-response; `response.cancel` errors `response_not_active` if idle (benign); insufficient_quota codes: no_billing_account, insufficient_quota, monthly_cap_reached, contract_ended; close 1013 = concurrency (backoff), 3000 = key, 4429 = billing.
- Voice create: docs conflict (`voice_id`/`title` in guide vs `voice`/`description` in schema) — client accepts either id field, sends `description`.

## API probe results
Pending: needs BOSON_API_KEY (probes a–f in brief §9 M0).
