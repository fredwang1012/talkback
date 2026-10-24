# One container serves the API, the cached audio and the built frontend. Not yet built or run: see docs/DEPLOY.md.
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend ./
RUN npm run build

FROM python:3.12-slim
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock backend/
# No embed extra (no torch): with RETRIEVAL_BACKEND=vertex, embeddings and search come from Vertex AI.
RUN cd backend && uv sync --frozen --no-dev
COPY backend backend
COPY --from=web /web/dist frontend/dist
# data/ is not in the image: mount a volume (Cloud Run: the GCS bucket) at /app/data.
ENV DATA_DIR=/app/data PORT=8000
EXPOSE 8000
# No uvicorn proxy-header flags: they would trust a client-supplied leftmost X-Forwarded-For. The app reads the
# right-most entry itself when TRUST_PROXY=true (set on Cloud Run). --no-sync skips the env check for fast cold starts.
CMD ["sh", "-c", "uv run --frozen --no-dev --no-sync --project backend uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
