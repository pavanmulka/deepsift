"""Search engines that turn a query into a ranked list of {title, url, snippet}.

- claude:  Claude's own WebSearch tool via `claude -p` (billed to the subscription)
- searxng: a self-hosted SearXNG instance (free, fastest)
- ddg:     DuckDuckGo's HTML page (free, no key; a fallback)
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from lxml import html as lxml_html

from .claude_cli import ClaudeError, ClaudeRunner
from .config import Settings


@dataclass
class Hit:
    title: str
    url: str
    snippet: str = ""
    published_date: Optional[str] = None


@dataclass
class Query:
    text: str
    max_results: int = 10
    topic: str = "general"           # general | news | finance
    time_range: Optional[str] = None  # day | week | month | year
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    include_domains: Optional[List[str]] = None
    exclude_domains: Optional[List[str]] = None
    country: Optional[str] = None
    language: Optional[str] = None
    safe_search: bool = False


class EngineError(RuntimeError):
    pass


_TIME = {"d": "day", "w": "week", "m": "month", "y": "year"}


def normalize_time_range(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _TIME.get(value, value)


_HIT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "snippet": {"type": "string"},
                    "published_date": {"type": ["string", "null"]},
                },
                "required": ["title", "url", "snippet"],
            },
        }
    },
    "required": ["results"],
}


class ClaudeEngine:
    name = "claude"

    def __init__(self, runner: ClaudeRunner, settings: Settings) -> None:
        self._runner = runner
        self._settings = settings

    def search(self, q: Query) -> List[Hit]:
        rules = []
        if q.include_domains:
            rules.append("Only return results from these domains: " + ", ".join(q.include_domains) + ".")
        if q.exclude_domains:
            rules.append("Never return results from: " + ", ".join(q.exclude_domains) + ".")
        tr = normalize_time_range(q.time_range)
        if tr:
            rules.append("Only results published in the past %s." % tr)
        if q.start_date or q.end_date:
            rules.append("Only results published between %s and %s." % (q.start_date or "any", q.end_date or "today"))
        if q.topic == "news":
            rules.append("Prefer recent news articles and include each one's published date.")
        elif q.topic == "finance":
            rules.append("Prefer financial sources (filings, market data, financial news).")
        if q.country:
            rules.append("Prefer results relevant to %s." % q.country)
        if q.language:
            rules.append("Prefer results written in %s." % q.language)
        prompt = (
            "Run the WebSearch tool for this exact query (one search; a second search only "
            "if the first returns fewer than %d useful results):\n\n%s\n\n%s\n"
            "Return up to %d results in the order the search ranked them. For each: the "
            "page title, the exact URL from the search results, a 1-3 sentence factual "
            "snippet about what that page says, and published_date (YYYY-MM-DD) only if "
            "the search result showed one, otherwise null."
        ) % (q.max_results, q.text, " ".join(rules), q.max_results)
        try:
            data = self._runner.run(
                prompt, model=self._settings.search_model, tools=["WebSearch"], schema=_HIT_SCHEMA
            )
        except ClaudeError as exc:
            raise EngineError("claude: %s" % exc) from exc
        rows = data.get("results") if isinstance(data, dict) else data
        hits = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url.startswith(("http://", "https://")):
                continue
            hits.append(Hit(
                title=str(row.get("title") or "").strip(),
                url=url,
                snippet=str(row.get("snippet") or "").strip(),
                published_date=row.get("published_date") or None,
            ))
        return hits


class SearxngEngine:
    name = "searxng"

    def __init__(self, settings: Settings) -> None:
        if not settings.searxng_url:
            raise EngineError("searxng: DEEPSIFT_SEARXNG_URL is not set")
        self._base = settings.searxng_url.rstrip("/")

    def search(self, q: Query) -> List[Hit]:
        text = q.text
        if q.include_domains and len(q.include_domains) <= 5:
            text += " (" + " OR ".join("site:" + d for d in q.include_domains) + ")"
        params: Dict[str, str] = {"q": text, "format": "json"}
        tr = normalize_time_range(q.time_range)
        if tr:
            params["time_range"] = tr
        if q.topic == "news":
            params["categories"] = "news"
        if q.language:
            params["language"] = q.language
        params["safesearch"] = "1" if q.safe_search else "0"
        hits: List[Hit] = []
        for page in (1, 2):
            params["pageno"] = str(page)
            try:
                response = httpx.get(self._base + "/search", params=params, timeout=15)
                response.raise_for_status()
                rows = response.json().get("results", [])
            except (httpx.HTTPError, ValueError) as exc:
                if hits:
                    break
                raise EngineError("searxng: %s" % exc) from exc
            for row in rows:
                hits.append(Hit(
                    title=row.get("title") or "",
                    url=row.get("url") or "",
                    snippet=row.get("content") or "",
                    published_date=(row.get("publishedDate") or "")[:10] or None,
                ))
            if len(hits) >= q.max_results or not rows:
                break
        return hits


class DuckDuckGoEngine:
    name = "ddg"
    _DF = {"day": "d", "week": "w", "month": "m", "year": "y"}

    # DuckDuckGo shows a bot check after a burst of requests; skip it for a while.
    COOLDOWN_SECONDS = 15 * 60

    def __init__(self, settings: Settings) -> None:
        self._ua = settings.user_agent
        self._blocked_until = 0.0

    def search(self, q: Query) -> List[Hit]:
        if time.time() < self._blocked_until:
            raise EngineError("ddg: cooling down after a bot check")
        text = q.text
        if q.include_domains and len(q.include_domains) <= 5:
            text += " " + " OR ".join("site:" + d for d in q.include_domains)
        form = {"q": text, "kp": "1" if q.safe_search else "-2"}
        tr = normalize_time_range(q.time_range)
        if tr in self._DF:
            form["df"] = self._DF[tr]
        try:
            response = httpx.post(
                "https://html.duckduckgo.com/html/",
                data=form,
                headers={"User-Agent": self._ua, "Referer": "https://html.duckduckgo.com/"},
                timeout=15,
                follow_redirects=True,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EngineError("ddg: %s" % exc) from exc
        hits = parse_ddg_html(response.text)
        if not hits and (response.status_code == 202 or "anomaly" in response.text.lower()):
            self._blocked_until = time.time() + self.COOLDOWN_SECONDS
            raise EngineError("ddg: blocked by a bot check")
        return hits


def parse_ddg_html(markup: str) -> List[Hit]:
    tree = lxml_html.fromstring(markup)
    hits = []
    for node in tree.xpath("//div[contains(@class,'result') and .//a[contains(@class,'result__a')]]"):
        if "result--ad" in (node.get("class") or ""):
            continue
        link = node.xpath(".//a[contains(@class,'result__a')]")[0]
        href = link.get("href") or ""
        if "duckduckgo.com/l/" in href:
            target = parse_qs(urlsplit(href).query).get("uddg", [""])[0]
            href = unquote(target)
        elif href.startswith("//"):
            href = "https:" + href
        if not href.startswith("http") or "duckduckgo.com/y.js" in href:
            continue
        snippet = node.xpath(".//*[contains(@class,'result__snippet')]")
        hits.append(Hit(
            title=link.text_content().strip(),
            url=href,
            snippet=snippet[0].text_content().strip() if snippet else "",
        ))
    # The outer and inner result divs both match; keep the first of each URL.
    seen, unique = set(), []
    for hit in hits:
        if hit.url not in seen:
            seen.add(hit.url)
            unique.append(hit)
    return unique


def build_engines(settings: Settings, runner: ClaudeRunner) -> list:
    engines = []
    for name in settings.backends:
        if name == "claude":
            engines.append(ClaudeEngine(runner, settings))
        elif name == "searxng" and settings.searxng_url:
            engines.append(SearxngEngine(settings))
        elif name == "ddg":
            engines.append(DuckDuckGoEngine(settings))
    if not engines:
        raise RuntimeError("no search engines configured (DEEPSIFT_BACKENDS)")
    return engines
