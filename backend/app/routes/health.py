"""Health and identity."""
from fastapi import APIRouter

from .. import deps, gemini, users
from ..config import settings


router = APIRouter()


@router.get("/api/health")
def health():
    return {"ok": True, "boson_key_set": bool(settings.boson_api_key), "access_required": bool(settings.access_token),
            "answer_backend": deps.answer_backend(),
            "llm": gemini.mode(),   # which Gemini endpoint: "vertex", "ai_studio" (GEMINI_API_KEY) or None; never the key
            # WEB_SEARCH off also takes Boson's server-side web_search away from the Realtime session (the browser declares it)
            "web_search": bool(settings.web_search), "avatar_size": settings.avatar_size}


@router.get("/api/me")
def me():
    ident = users.current()
    return {"email": ident.email if settings.per_user else None, "per_user": settings.per_user}
