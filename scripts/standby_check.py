"""Check the always-on Apify address with Tavily's own Python library and the MCP endpoint.

Run:  APIFY_TOKEN=$(apify auth token) .venv/bin/python scripts/standby_check.py
The token is only sent to Apify and is never printed.
"""
import json
import os
import sys
import time

import httpx
from tavily import TavilyClient

BASE = os.environ.get("DEEPSIFT_STANDBY_URL", "https://aadstra--deepsift-search.apify.actor")
token = os.environ.get("APIFY_TOKEN", "")
if not token:
    sys.exit("Set APIFY_TOKEN first (see the top of this file).")

client = TavilyClient(api_key=token, api_base_url=BASE)
for label, kwargs in [("cold start", {}), ("warm", {}), ("advanced", {"search_depth": "advanced"})]:
    started = time.time()
    result = client.search("who acquired Tavily in 2026", max_results=3, **kwargs)
    print("search (%s): %.1fs, %d results" % (label, time.time() - started, len(result["results"])))
    for row in result["results"]:
        print("   -", row["url"], "|", row["content"][:100].replace("\n", " "))

started = time.time()
extract = client.extract(["https://docs.apify.com/platform/actors/running/standby"], query="readiness probe")
print("extract: %.1fs, %d page(s)" % (time.time() - started, len(extract["results"])))

headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
           "MCP-Protocol-Version": "2025-06-18"}
listing = httpx.post(BASE + "/mcp", headers=headers, timeout=60,
                     json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
tools = [t["name"] for t in listing.json()["result"]["tools"]]
print("mcp tools:", tools)
print("ALL CHECKS PASSED")
