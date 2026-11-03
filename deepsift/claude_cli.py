"""Runs `claude -p` so every model call is billed to the signed-in Claude subscription.

ANTHROPIC_API_KEY is removed from the child environment on purpose: if it were
present, Claude Code would bill the API account instead of the subscription.
For a long-running service, sign in once with `claude setup-token` and put the
token in CLAUDE_CODE_OAUTH_TOKEN.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from typing import Any, Dict, List, Optional

from .config import Settings

_SYSTEM = (
    "You are a non-interactive backend for a web search API. Follow the user "
    "instructions exactly and return only the requested data. Never invent URLs, "
    "titles, dates or facts that the tools did not return."
)
# Session variables from a parent Claude Code process confuse a child CLI.
_DROP_PREFIXES = ("CLAUDE_CODE_", "CLAUDE_AGENT_SDK_")
_DROP_KEYS = {"CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"}
_KEEP_KEYS = {"CLAUDE_CODE_OAUTH_TOKEN"}


class ClaudeError(RuntimeError):
    pass


class ClaudeRunner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._slots = threading.BoundedSemaphore(settings.claude_concurrency)
        self._cwd = settings.data_dir / "claude-cwd"
        self._cwd.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.calls = 0
        self.web_searches = 0
        self.web_fetches = 0

    def _env(self) -> Dict[str, str]:
        env = {}
        for key, value in os.environ.items():
            if key in _KEEP_KEYS:
                env[key] = value
            elif key in _DROP_KEYS or key.startswith(_DROP_PREFIXES):
                continue
            else:
                env[key] = value
        return env

    def run(
        self,
        prompt: str,
        *,
        model: str,
        tools: Optional[List[str]] = None,
        schema: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        """Return structured output when `schema` is given, else the reply text."""
        tools = tools or []
        cmd = [
            self._settings.claude_bin, "-p",
            "--model", model,
            "--output-format", "json",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--setting-sources", "project",
            "--disable-slash-commands",
            "--system-prompt", _SYSTEM,
            "--tools", ",".join(tools) if tools else "",
        ]
        if tools:
            cmd += ["--allowedTools", ",".join(tools)]
        if schema is not None:
            cmd += ["--json-schema", json.dumps(schema)]
        with self._slots:
            try:
                proc = subprocess.run(
                    cmd,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    cwd=str(self._cwd),
                    env=self._env(),
                    timeout=timeout or self._settings.claude_timeout,
                )
            except FileNotFoundError as exc:
                raise ClaudeError("claude CLI not found; set DEEPSIFT_CLAUDE_BIN") from exc
            except subprocess.TimeoutExpired as exc:
                raise ClaudeError("claude CLI timed out") from exc
        try:
            payload = json.loads(proc.stdout)
        except ValueError as exc:
            detail = (proc.stderr or proc.stdout or "").strip()[:300]
            raise ClaudeError("claude CLI returned no JSON: " + detail) from exc
        server_tools = (payload.get("usage") or {}).get("server_tool_use") or {}
        with self._lock:
            self.calls += 1
            self.web_searches += int(server_tools.get("web_search_requests") or 0)
            self.web_fetches += int(server_tools.get("web_fetch_requests") or 0)
        if payload.get("is_error"):
            raise ClaudeError(str(payload.get("result") or "claude CLI error")[:300])
        if schema is None:
            return str(payload.get("result") or "").strip()
        structured = payload.get("structured_output")
        if structured is not None:
            return structured
        return parse_json_text(str(payload.get("result") or ""))


def parse_json_text(text: str) -> Any:
    """Pull the first JSON object or array out of a model reply."""
    fenced = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    if fenced:
        text = fenced.group(1)
    for opener in ("{", "["):
        start = text.find(opener)
        if start < 0:
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(text[start:])
            return value
        except ValueError:
            continue
    raise ClaudeError("model reply was not JSON")
