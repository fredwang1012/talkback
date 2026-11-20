"""Upload limits and the error an upload can fail with (shared by the whole package)."""


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MULTIPART_SLACK = 1024 * 1024  # form-encoding overhead allowed on top of the file cap when checking Content-Length
MAX_PAGES = 2000
MAX_CHARS = 5_000_000
MAX_PARAGRAPH_CHARS = 5_000
NARRATOR_VOICE = "marcus"
LICENSE = "Uploaded by the user"


class UploadError(Exception):
    """A problem with the uploaded file that the user can act on."""
