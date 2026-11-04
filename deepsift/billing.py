"""Which pay-per-event charges one finished request earns on the hosted Apify Actor.

Event names must match the events set up under Publishing > Monetization in Apify Console.
Nothing is charged for failed requests.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

SEARCH = "search"                    # basic / fast / ultra-fast search
ADVANCED_SEARCH = "advanced-search"  # search_depth=advanced (more passages, longer page reads)
PAGE_READ = "page-read"              # each page returned by extract or crawl
SITE_MAP = "site-map"                # one map request

ALL_EVENTS = (SEARCH, ADVANCED_SEARCH, PAGE_READ, SITE_MAP)


def events_for(endpoint: str, body: Dict[str, Any], result: Dict[str, Any]) -> List[Tuple[str, int]]:
    if endpoint == "search":
        advanced = (body.get("search_depth") or "basic") == "advanced"
        return [(ADVANCED_SEARCH if advanced else SEARCH, 1)]
    if endpoint in ("extract", "crawl"):
        pages = len(result.get("results") or [])
        return [(PAGE_READ, pages)] if pages else []
    if endpoint == "map":
        return [(SITE_MAP, 1)]
    return []
