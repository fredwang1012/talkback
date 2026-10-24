# Deploying Talkback (optional, M10)

One container serves everything: the FastAPI API, cached audio (`/media`), portraits, and the built frontend (the
backend mounts `frontend/dist` at `/` when it exists). Same origin, so no CORS settings and no extra proxy.

Status: the Dockerfile is written but **not built or run yet**. Treat the first build as part of the work.

## Railway (or Fly/Render)

1. New service from this repo, root `talkback/`, builder: Dockerfile.
2. Variables: `BOSON_API_KEY` (required), `TALKBACK_ACCESS_TOKEN` (recommended for a public URL: visitors must type the
   code before voices can spend credit), `PORT` is provided by the platform.
3. Add a **volume** mounted at `/app/data/cache` so synthesised audio survives redeploys. Pre-render before the demo
   (`preprocess/prerender_tts.py`) on the deployed volume, or copy `data/cache/tts` into it.
4. Health check path: `/api/health`.

## Before sharing a public URL

- Set a monthly spend cap in the Boson workspace.
- Keep `TALKBACK_ACCESS_TOKEN` set. Rate limits (token mint 10/min, TTS cache misses 120/min, clone 5/min) are per IP.
- Demo mode (`?demo=1`) serves pre-rendered audio only and never calls Boson for speech.

## Image size

`sentence-transformers` pulls in torch (large). If the image is too big for the platform, deploy with the BM25-only
fallback by building without `--extra embed`: retrieval still works in English, multilingual questions get weaker.
