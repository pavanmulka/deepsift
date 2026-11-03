#!/bin/sh
# Removes the tools from Claude Code and the deepsift command. The folder itself is left alone.
command -v claude >/dev/null 2>&1 && claude mcp remove --scope user deepsift
rm -f "$HOME/.local/bin/deepsift"
echo "Removed. Delete this folder to remove everything, including the cache in ~/.deepsift."
