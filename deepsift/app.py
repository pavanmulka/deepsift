"""The local Tavily-compatible HTTP API that run.sh serves (see deepsift/api.py)."""
from __future__ import annotations

from . import config
from .api import create_app
from .service import Service

settings = config.load()
service = Service(settings)
app = create_app(service, settings)
