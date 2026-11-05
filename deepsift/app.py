"""The local Tavily-compatible HTTP API that run.sh serves (see deepsift/api.py)."""
from __future__ import annotations

from . import config
from .api import create_app
from .service import Service

settings = config.load()
service = Service(settings)
app = create_app(service, settings)

if service.reranker is not None:
    # Load the AI scorer in the background so the first search doesn't wait for it.
    import threading

    threading.Thread(target=service.reranker.warm, daemon=True).start()
