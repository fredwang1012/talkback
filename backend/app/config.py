from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # talkback/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    boson_api_key: str = ""
    anthropic_api_key: str = ""
    boson_base_url: str = "https://api.boson.ai"
    data_dir: Path = ROOT / "data"
    # Optional shared secret. When set, endpoints that spend Boson credit require header X-Talkback-Token.
    # Env var is TALKBACK_ACCESS_TOKEN (README, DEPLOY.md); plain ACCESS_TOKEN still works.
    access_token: str = Field(default="", validation_alias=AliasChoices("TALKBACK_ACCESS_TOKEN", "ACCESS_TOKEN"))
    # True only behind a trusted proxy (Cloud Run): client_ip then uses the right-most X-Forwarded-For entry.
    trust_proxy: bool = False
    # Comma-separated browser origins allowed by CORS (the single-container deploy is same-origin and needs none).
    cors_origins: str = "http://localhost:5173"
    # Google Cloud (Vertex AI). retrieval_backend="local" keeps today's BM25 + e5 behaviour.
    gcp_project: str = ""  # set GCP_PROJECT when RETRIEVAL_BACKEND=vertex or UPLOAD_LLM=gemini
    gcp_region: str = "us-central1"
    gcp_bucket: str = ""
    retrieval_backend: Literal["local", "vertex"] = "local"
    # Who writes talk-mode answers. None = follow retrieval_backend (vertex -> gemini, local -> realtime).
    answer_backend: Literal["gemini", "realtime"] | None = None
    gemini_model: str = "gemini-2.5-flash"
    vs_collection: str = "chunks"

    # Uploaded novels: cast, dialogue split and summaries by an LLM after the text ingest (app/enrich).
    usage_log_file: bool = True       # append every billable call to data/cache/usage.jsonl (off on Cloud Run: logs only)
    upload_enrich: bool = True
    upload_llm: Literal["claude", "gemini"] = "claude"   # developer-only; claude falls back to gemini when out of credit
    upload_confirm: bool = False      # testing mode: show the estimate and wait for POST /api/books/{id}/enrich
    upload_concurrency: int = 24      # parallel LLM calls during enrichment
    upload_book_cap_usd: float = 4.0      # refuse a book whose estimate is above this
    upload_total_cap_usd: float = 15.0    # hard stop on cumulative upload enrichment spend (data/usage)

    # Public processing API (see docs/DEPLOY.md). PUBLIC_API=true runs the server side: only /api/health and
    # /api/public/*, Gemini only. PUBLIC_API_URL (client side, set in .env.example) points a local app at it.
    public_api: bool = False
    public_api_url: str = ""
    public_book_cap_usd: float = 1.5            # refuse a book whose estimate is above this
    public_total_cap_usd: float = 10.0          # cumulative enrichment spend, persisted
    public_ip_daily_cap_usd: float = 2.0        # enrichment estimates per client address per UTC day, persisted
    public_answer_daily_cap_usd: float = 2.0    # Gemini answers per UTC day, persisted

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def effective_answer_backend(self) -> str:
        return self.answer_backend or ("gemini" if self.retrieval_backend == "vertex" else "realtime")

    @property
    def vs_collection_path(self) -> str:
        return f"projects/{self.gcp_project}/locations/{self.gcp_region}/collections/{self.vs_collection}"

    def require_boson_key(self) -> str:
        if not self.boson_api_key:
            raise RuntimeError(
                "BOSON_API_KEY is not set. Copy talkback/.env.example to talkback/.env and fill it in."
            )
        return self.boson_api_key


settings = Settings()
