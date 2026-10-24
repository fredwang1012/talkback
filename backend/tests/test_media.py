from fastapi.testclient import TestClient

from app import main


def test_media_serves_only_tts_audio():
    """data/cache also holds the usage log and the LLM cache; neither may be reachable through /media."""
    client = TestClient(main.app)
    for path in ("/media/usage.jsonl", "/media/cache/usage.jsonl", "/media/llm/x.json", "/media/../cache/usage.jsonl"):
        assert client.get(path).status_code == 404
