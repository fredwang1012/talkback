from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # talkback/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    boson_api_key: str = Field(default="", repr=False)   # never in a repr (tracebacks, test output)
    anthropic_api_key: str = Field(default="", repr=False)
    boson_base_url: str = "https://api.boson.ai"
    data_dir: Path = ROOT / "data"
    # Optional shared secret. When set, endpoints that spend Boson credit require header X-Talkback-Token.
    # Env var is TALKBACK_ACCESS_TOKEN (README, DEPLOY.md); plain ACCESS_TOKEN still works.
    access_token: str = Field(default="", repr=False, validation_alias=AliasChoices("TALKBACK_ACCESS_TOKEN", "ACCESS_TOKEN"))
    # True only behind a trusted proxy (Cloud Run): client_ip then uses the right-most X-Forwarded-For entry.
    trust_proxy: bool = False
    # Comma-separated browser origins allowed by CORS (the single-container deploy is same-origin and needs none).
    cors_origins: str = "http://localhost:5173"
    # Google Cloud (Vertex AI). retrieval_backend="local" keeps today's BM25 + e5 behaviour.
    gcp_project: str = ""  # set GCP_PROJECT when RETRIEVAL_BACKEND=vertex or UPLOAD_LLM=gemini
    gcp_region: str = "us-central1"
    # Gemini 3 on a new project is served from "global", not us-central1. Empty uses gcp_region.
    gemini_location: str = ""
    gcp_bucket: str = ""
    retrieval_backend: Literal["local", "vertex"] = "local"
    # Who writes talk-mode answers. None = follow retrieval_backend (vertex -> gemini, local -> realtime).
    answer_backend: Literal["gemini", "realtime"] | None = None
    gemini_model: str = "gemini-2.5-flash"
    # Web search (Grounding with Google Search) on Gemini answers: textbooks and novels with spoilers on only (websearch.py).
    web_search: bool = True
    grounded_daily_cap: int = 100        # grounded answers per UTC day on this app, persisted (data/usage)
    public_grounded_daily_cap: int = 50  # public service: grounded answers per UTC day, on top of the answer budget

    def gemini_region(self) -> str:
        return self.gemini_location or self.gcp_region
    vs_collection: str = "chunks"

    # Uploaded novels: cast, dialogue split and summaries by an LLM after the text ingest (app/enrich).
    usage_log_file: bool = True       # append every billable call to data/cache/usage.jsonl (off on Cloud Run: logs only)
    upload_enrich: bool = True
    # Re-chunk (and save) a book whose tts_chunks were built with older rules (ttschunks.CHUNK_RULES_VERSION), in a
    # background thread at startup / on first load. No LLM, no API call; changed chunks are re-voiced when played.
    auto_rechunk: bool = True
    minor_voices: bool = True  # uncast speakers in uploaded novels read in one of two generic voices (enrich/minor.py)
    upload_llm: Literal["claude", "gemini"] = "claude"   # developer-only; claude falls back to gemini when out of credit
    upload_confirm: bool = False      # testing mode: show the estimate and wait for POST /api/books/{id}/enrich
    upload_concurrency: int = 24      # parallel LLM calls during enrichment
    enrich_parallel: int = 1000       # uploads processed at the same time: no practical limit; lower it if Gemini rate limits bite
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

    # Per-account separation for the live Cloud Run deploy (Google sign-in via IAP). Off everywhere else.
    per_user: bool = False
    iap_audience: str = ""        # /projects/PROJECT_NUMBER/locations/REGION/services/SERVICE
    owner_emails: str = ""        # comma list; the first one inherits the pre-existing shared data
    owner_only_books: str = ""    # comma list of globs on book ids visible only to owner emails (e.g. "hp*")

    @model_validator(mode="after")
    def _per_user_needs_audience(self):
        if self.per_user and not self.iap_audience.strip():
            raise ValueError("PER_USER=true requires IAP_AUDIENCE (/projects/NUMBER/locations/REGION/services/NAME); "
                             "without it every visitor would be anonymous.")
        return self

    @property
    def owner_email_list(self) -> list[str]:
        return [e.strip().lower() for e in self.owner_emails.split(",") if e.strip()]

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
