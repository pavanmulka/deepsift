#!/bin/sh
# Optional: run the Tavily-compatible HTTP API on 127.0.0.1:8787 (override with HOST / PORT).
# Claude Code does not need this; it starts the tools itself.
set -e
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || ./install.sh --no-register
exec .venv/bin/python -W ignore -m uvicorn deepsift.app:app \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8787}" --workers 1
