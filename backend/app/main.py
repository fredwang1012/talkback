from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import books
from .config import settings
from .schema import Book

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


@app.get("/api/books")
def get_books():
    return books.list_books()


@app.get("/api/books/{book_id}", response_model=Book)
def get_book(book_id: str):
    book = books.load_book(book_id)
    if book is None:
        raise HTTPException(404, "Book not found")
    return book
