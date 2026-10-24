"""Container entry point: `app.asgi:app`. PUBLIC_API=true serves only the public processing API (and never imports the
main app, its static files or its book data); otherwise the normal Talkback app."""
from .config import settings

if settings.public_api:
    from .public_api import app
else:
    from .main import app

__all__ = ["app"]
