from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import settings

app = FastAPI(title="Bookson")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

(settings.data_dir / "cache").mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=settings.data_dir / "cache"), name="media")


@app.get("/api/health")
def health():
    return {"ok": True, "boson_key_set": bool(settings.boson_api_key)}
