#!/bin/sh
# One-command setup: ./install.sh
#   1. builds a private Python environment in .venv
#   2. adds the web tools to Claude Code (all projects)
#   3. adds the `deepsift` command to ~/.local/bin
# Run it again any time; it is safe to repeat. ./install.sh --no-register skips steps 2 and 3.
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"
REGISTER=1
[ "$1" = "--no-register" ] && REGISTER=0

say() { printf '\n==> %s\n' "$1"; }

say "Finding Python 3.10 or newer"
PY=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$candidate")"
    break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ not found. On a Mac: brew install python@3.12   then run ./install.sh again."
  exit 1
fi
echo "Using $PY ($("$PY" --version))"

say "Installing packages into .venv"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  rm -rf .venv
  "$PY" -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
chmod +x bin/deepsift bin/deepsift-mcp

say "Checking it can read a web page"
if bin/deepsift read https://example.com >/dev/null 2>&1; then
  echo "OK"
else
  echo "Could not read https://example.com. Check the internet connection, then run ./install.sh again."
  exit 1
fi

if [ "$REGISTER" = 1 ]; then
  say "Adding the tools to Claude Code"
  if command -v claude >/dev/null 2>&1; then
    claude mcp remove --scope user deepsift >/dev/null 2>&1 || true
    claude mcp add --scope user deepsift -- "$ROOT/bin/deepsift-mcp"
  else
    echo "Claude Code (the 'claude' command) is not installed. Install it from https://claude.com/claude-code, then run ./install.sh again."
  fi

  say "Adding the deepsift command"
  mkdir -p "$HOME/.local/bin"
  ln -sf "$ROOT/bin/deepsift" "$HOME/.local/bin/deepsift"
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) echo "deepsift is ready." ;;
    *) echo "Add this line to ~/.zshrc so your terminal finds deepsift:  export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac

  say "Checking the Claude sign-in"
  if command -v claude >/dev/null 2>&1 && claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    echo "Signed in. Searches will use Claude's web search."
  else
    echo "Not signed in. Run:  claude auth login"
    echo "(Until then, searches use the free DuckDuckGo fallback, which slows down after a burst of searches.)"
  fi
fi

say "Done. Open a new Claude Code session and ask, for example: \"use web_search to find ...\""
