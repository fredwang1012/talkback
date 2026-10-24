from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # talkback/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    boson_api_key: str = ""
    anthropic_api_key: str = ""
    boson_base_url: str = "https://api.boson.ai"
    data_dir: Path = ROOT / "data"
    # Optional shared secret. When set, endpoints that spend Boson credit require header X-Talkback-Token.
    access_token: str = ""
    # Google Cloud (Vertex AI). retrieval_backend="local" keeps today's BM25 + e5 behaviour.
    gcp_project: str = "frederick-talkback"
    gcp_region: str = "us-central1"
    gcp_bucket: str = "frederick-talkback-data"
    retrieval_backend: Literal["local", "vertex"] = "local"
    gemini_model: str = "gemini-2.5-flash"
    vs_collection: str = "chunks"

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
