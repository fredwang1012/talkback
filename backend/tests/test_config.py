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
