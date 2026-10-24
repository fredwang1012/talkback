from app.config import Settings


def test_cors_origins_default_and_comma_separated(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings(_env_file=None).cors_origin_list == ["http://localhost:5173"]
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:5173, https://talkback.example.com ,")
    assert Settings(_env_file=None).cors_origin_list == ["http://localhost:5173", "https://talkback.example.com"]


def test_access_token_read_from_talkback_prefixed_env(monkeypatch):
    monkeypatch.delenv("ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("TALKBACK_ACCESS_TOKEN", "abc123")
    assert Settings(_env_file=None).access_token == "abc123"


def test_upload_llm_defaults_to_gemini():
    """No Claude key is configured anywhere; the Claude path stays for anyone who sets UPLOAD_LLM=claude."""
    from app.config import Settings
    assert Settings.model_fields["upload_llm"].default == "gemini"


def test_preprocess_provider_defaults_to_gemini(monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocess"))
    import llm as pre_llm
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert pre_llm.provider() == "gemini"
