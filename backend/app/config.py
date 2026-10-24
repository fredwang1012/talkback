from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]  # bookson/


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    boson_api_key: str = ""
    anthropic_api_key: str = ""
    boson_base_url: str = "https://api.boson.ai"
    data_dir: Path = ROOT / "data"

    def require_boson_key(self) -> str:
        if not self.boson_api_key:
            raise RuntimeError(
                "BOSON_API_KEY is not set. Copy bookson/.env.example to bookson/.env and fill it in."
            )
        return self.boson_api_key


settings = Settings()
