"""Cast settings per book."""
from fastapi import APIRouter, Depends, HTTPException

from .. import books, cast, deps, users
from ..schema import Book


router = APIRouter()


def _book_for_cast(book_id: str) -> Book:
    book = books.load_book(book_id) if deps._SAFE_NAME.match(book_id) and users.can_see_book(book_id) else None
    if book is None:
        raise HTTPException(404, "Book not found")
    return book


@router.get("/api/books/{book_id}/cast", dependencies=[Depends(deps.require_access)])
def get_cast(book_id: str):
    return cast.load(_book_for_cast(book_id))


@router.put("/api/books/{book_id}/cast", dependencies=[Depends(deps.require_access)])
def put_cast(book_id: str, body: cast.CastSettings):
    try:
        return cast.save(_book_for_cast(book_id), body)
    except cast.CastError as e:
        raise HTTPException(400, str(e))
