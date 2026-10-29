"""Which Gemini endpoint the app talks to (#137), and its google-genai client.

- "vertex": Vertex AI with Application Default Credentials (GCP_PROJECT). The Cloud Run deploy, unchanged.
- "ai_studio": a Google AI Studio API key (GEMINI_API_KEY), same SDK and model ids, for a host with no Google Cloud
  project or credits. Used only when Vertex is not usable: no GCP_PROJECT, or ADC that do not load.
- None: neither; Gemini features fall back as before (the public processing service, Realtime answers, no translation).

GCP_PROJECT without GEMINI_API_KEY stays "vertex" without probing ADC (today's behaviour). The key is never logged
or returned (Settings.gemini_api_key has repr=False; health reports only the mode).
"""
import functools
from typing import Literal

from .config import settings

Mode = Literal["vertex", "ai_studio"]


@functools.lru_cache(maxsize=1)
def gcp_credentials_usable() -> bool:
    """A GCP project is configured and Application Default Credentials load (checked once)."""
    if not settings.gcp_project:
        return False
    try:
        import google.auth
        google.auth.default()
        return True
    except Exception:
        return False


def mode() -> Mode | None:
    if settings.gcp_project and (not settings.gemini_api_key or gcp_credentials_usable()):
        return "vertex"
    if settings.gemini_api_key:
        return "ai_studio"
    return None


def available() -> bool:
    return mode() is not None


def client():
    """A google-genai client for the chosen endpoint (Vertex in the Gemini region, else AI Studio with the key)."""
    from google import genai
    if mode() == "ai_studio":
        return genai.Client(api_key=settings.gemini_api_key)
    return genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gemini_region())
