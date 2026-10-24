"""ASGI middleware that bounds the upload request body before the multipart parser sees it.

For POST /api/books/upload: Transfer-Encoding is refused (400), Content-Length is required (411) and must be a
plain number not above cap + form overhead (413). The body bytes are counted as they arrive, so the cap holds even
if the headers lie: once exceeded, the app sees a disconnect and the client gets 413.
"""
import json

from . import ingest_upload

PATH = "/api/books/upload"


class UploadSizeLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != PATH:
            return await self.app(scope, receive, send)
        headers = {k.lower(): v for k, v in scope["headers"]}
        limit = ingest_upload.MAX_UPLOAD_BYTES + ingest_upload.MULTIPART_SLACK
        if b"transfer-encoding" in headers:
            return await _reply(send, 400, "Transfer-Encoding is not supported for uploads.")
        declared = headers.get(b"content-length")
        if declared is None:
            return await _reply(send, 411, "Content-Length is required.")
        if not (declared.isascii() and declared.isdigit()):
            return await _reply(send, 400, "Invalid Content-Length.")
        if int(declared) > limit:
            return await _reply(send, 413, "File is too large (20 MB maximum).")

        received = 0
        exceeded = False
        responded = False

        async def counting_receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message):
            nonlocal responded
            if exceeded:  # drop whatever the app says about a truncated body; we answer 413 ourselves
                return
            if message["type"] == "http.response.start":
                responded = True
            await send(message)

        try:
            await self.app(scope, counting_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not responded:
            await _reply(send, 413, "File is too large (20 MB maximum).")


async def _reply(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
