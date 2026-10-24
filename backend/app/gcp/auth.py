"""Application Default Credentials bearer tokens for Google Cloud REST calls."""
import threading

import google.auth
import google.auth.transport.requests

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

_lock = threading.Lock()
_credentials = None


def get_token() -> str:
    """Return a valid access token, loading ADC once and refreshing when invalid."""
    global _credentials
    with _lock:
        if _credentials is None:
            _credentials, _ = google.auth.default(scopes=SCOPES)
        if not _credentials.valid:
            _credentials.refresh(google.auth.transport.requests.Request())
        return _credentials.token
