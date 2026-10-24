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
# CPU-only torch keeps the image small; the embed extra is needed for multilingual retrieval at runtime
RUN cd backend && uv sync --frozen --extra embed --no-dev
COPY backend backend
COPY data data
COPY --from=web /web/dist frontend/dist
ENV DATA_DIR=/app/data PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uv run --project backend uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
