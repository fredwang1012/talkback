# Talkback — project conventions

The original spec and build logs are in `docs/internal/` (`BRIEF.md`, `NOTES.md`, `BLOCKED.md`); judges do not need them. Log new findings in `docs/internal/NOTES.md`.

## Commands
- Backend (Python 3.12, uv): `cd backend && uv sync && uv run uvicorn app.main:app --port 8000`; tests `uv run pytest`
- Frontend: `cd frontend && npm install && npm run dev` (proxies /api and /media to :8000); tests `npm test`; types `npx tsc -p tsconfig.app.json --noEmit`
- Keys live in `.env` at the repo root (gitignored). Never put BOSON_API_KEY / ANTHROPIC_API_KEY in frontend code, logs, or git.

## Invariants
- Spoiler rule: retrieval chunk eligible only if `para_end <= reader_para`; summaries `upto_para <= reader_para`. Enforced in the backend and unit-tested.
- TTS input chunks <= 280 chars, sentence-aligned; same voice per speaker on every chunk.
- Realtime: mint a fresh ephemeral key per connection; `connect()` must be idempotent (StrictMode); tolerate unknown events.
- LLM work (uploaded-novel enrichment, offline preprocess, evals) runs on Gemini via Vertex (`UPLOAD_LLM=gemini`, `LLM_PROVIDER=gemini`); the Anthropic credit is gone and no Claude key is configured. The Claude provider code stays for anyone who sets ANTHROPIC_API_KEY.
- Branch on `error.type`: retry `rate_limit_error`/5xx; never retry `insufficient_quota`.
- Files copied from the Boson tutorial keep their Apache-2.0 header (see `frontend/THIRD_PARTY_NOTICES.md`).
