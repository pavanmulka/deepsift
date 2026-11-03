#!/usr/bin/env python3
"""Stands in for `claude -p` in tests: prints the JSON shape the real CLI prints."""
import json
import os
import sys

args = sys.argv[1:]
prompt = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as handle:
        handle.write(json.dumps({"args": args, "prompt": prompt,
                                 "has_api_key": "ANTHROPIC_API_KEY" in os.environ}) + "\n")

tools = args[args.index("--tools") + 1] if "--tools" in args else ""
out = {"type": "result", "is_error": False, "result": "", "usage": {"server_tool_use": {}}}
if "--json-schema" in args and "WebFetch" in tools:
    out["structured_output"] = {
        "content": "Databricks was founded in 2013 [1].",
        "sources": [{"title": "Databricks - Wikipedia", "url": "https://en.wikipedia.org/wiki/Databricks"}],
    }
    out["usage"]["server_tool_use"] = {"web_search_requests": 3, "web_fetch_requests": 1}
elif "--json-schema" in args:
    out["structured_output"] = {"results": [
        {"title": "Example Domain", "url": "https://example.com/", "snippet": "Example snippet.",
         "published_date": None},
        {"title": "Not a URL", "url": "javascript:alert(1)", "snippet": "dropped"},
    ]}
    out["usage"]["server_tool_use"] = {"web_search_requests": 1}
else:
    out["result"] = "A short answer."
print(json.dumps(out))
