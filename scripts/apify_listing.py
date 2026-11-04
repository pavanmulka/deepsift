"""Set the Apify Store search-engine title/description for DeepSift Search.

The icon must be uploaded in Apify Console (Source > Publication); the API refuses outside picture links.

Run:  APIFY_TOKEN=$(apify auth token) .venv/bin/python scripts/apify_listing.py
The token is only sent to api.apify.com and is never printed.
"""
import os
import sys

import httpx

ACTOR = "aadstra~deepsift-search"
token = os.environ.get("APIFY_TOKEN") or sys.exit("Set APIFY_TOKEN first (see the top of this file).")
changes = {
    "seoTitle": "Cheap Tavily alternative: web search API for AI agents",
    "seoDescription": "Google search API for AI agents and RAG that returns only the passages that answer "
                      "the question. Tavily-compatible, MCP for Claude. From $4 per 1,000.",
}
r = httpx.put("https://api.apify.com/v2/acts/" + ACTOR, json=changes, timeout=30,
              headers={"Authorization": "Bearer " + token})
print("status", r.status_code, "" if r.is_success else r.text[:400])
data = r.json().get("data") or r.json()
for key in changes:
    print(key, "=>", data.get(key))
