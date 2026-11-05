"""Search engines that turn a query into a ranked list of {title, url, snippet}.

- claude:  Claude's own WebSearch tool via `claude -p` (billed to the subscription)
- searxng: a self-hosted SearXNG instance (free, fastest)
- ddg:     DuckDuckGo's HTML page (free, no key; a fallback)
- google:  Google through Apify's GOOGLE_SERP proxy (used by the hosted Apify Actor)
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
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
        self._engines = settings.searxng_engines.strip()

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
        if self._engines:
            params["engines"] = self._engines
        targets = site_targets(q.text)
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
            found = only_sites([Hit(
                title=row.get("title") or "",
                url=row.get("url") or "",
                snippet=row.get("content") or "",
                published_date=(row.get("publishedDate") or "")[:10] or None,
            ) for row in rows], targets)
            known = {h.url for h in hits}
            hits.extend(h for h in found if h.url and h.url not in known)
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


class ApifySerpEngine:
    """Google results through Apify's GOOGLE_SERP proxy (billed per results page to the Apify account).

    The proxy only accepts plain-HTTP requests to www.google.* and returns the results page HTML.
    """
    name = "google"
    _TBS = {"day": "qdr:d", "week": "qdr:w", "month": "qdr:m", "year": "qdr:y"}
    # Service asks for max_results + 3 head-room; a second Google page is only worth paying for
    # when the caller wants more than one page (10) of results.
    ONE_PAGE = 13

    def __init__(self, settings: Settings) -> None:
        if not settings.serp_proxy_url:
            raise EngineError("google: no Apify GOOGLE_SERP proxy configured")
        self._proxy = settings.serp_proxy_url
        self._ua = settings.user_agent
        self._timeout = settings.google_timeout
        self._retries = max(0, settings.google_retries)
        self.pages_fetched = 0
        self.last_html = ""  # the last results page, kept so empty searches can be diagnosed

    def _params(self, q: Query, start: int) -> Dict[str, str]:
        text = q.text
        if q.include_domains and len(q.include_domains) <= 5:
            text += " (" + " OR ".join("site:" + d for d in q.include_domains) + ")"
        if q.exclude_domains:
            text += " " + " ".join("-site:" + d for d in q.exclude_domains[:5])
        params: Dict[str, str] = {"q": text}
        tbs = []
        tr = normalize_time_range(q.time_range)
        if tr in self._TBS:
            tbs.append(self._TBS[tr])
        if q.start_date or q.end_date:
            lo = _us_date(q.start_date)
            hi = _us_date(q.end_date)
            tbs.append("cdr:1,cd_min:%s,cd_max:%s" % (lo, hi))
        if tbs:
            params["tbs"] = ",".join(tbs)
        if q.topic == "news":
            params["tbm"] = "nws"
        elif q.include_domains or site_targets(q.text):
            # When few pages match a site: search, Google's normal view fills up with other websites;
            # the plain "Web" view (udm=14) shows only the real matches.
            params["udm"] = "14"
        # The proxy picks an exit country at random, so always say which Google to ask.
        code = ""
        if q.country:
            code = _COUNTRY_CODES.get(q.country.lower(), q.country if len(q.country) == 2 else "")
        params["gl"] = (code or "us").lower()
        params["hl"] = q.language or "en"
        params["safe"] = "active" if q.safe_search else "off"
        if start:
            params["start"] = str(start)
        return params

    def _page(self, q: Query, start: int) -> List[Hit]:
        """One results page; tries again after a timeout, a proxy error or a page with no results."""
        last: Optional[Exception] = None
        for _ in range(1 + self._retries):
            try:
                response = httpx.get(
                    "http://www.google.com/search",
                    params=self._params(q, start),
                    headers={"User-Agent": self._ua, "Accept-Language": (q.language or "en") + ";q=0.9"},
                    proxy=self._proxy,
                    timeout=self._timeout,
                    follow_redirects=True,
                )
                self.pages_fetched += 1
                response.raise_for_status()
            except httpx.HTTPError as exc:
                last = exc
                continue
            self.last_html = response.text
            parsed = parse_google_html(response.text)
            if parsed or start:
                return parsed
            last = None  # an empty first page: worth one more look
        if last is not None:
            raise EngineError("google: %s" % last)
        return []

    def search(self, q: Query) -> List[Hit]:
        hits: List[Hit] = []
        pages = 1 if q.max_results <= self.ONE_PAGE else 2
        for page in range(pages):
            try:
                parsed = self._page(q, page * 10)
            except EngineError:
                if hits:
                    break
                raise
            found = only_sites(resolve_goto_links(parsed, self._ua), site_targets(q.text))
            known = {h.url for h in hits}
            hits.extend(h for h in found if h.url not in known)
            if len(found) < 5:
                break
        return hits


_SITE = re.compile(r"(?<![\w-])site:(\S+)", re.I)


def site_targets(text: str) -> List[Tuple[str, str]]:
    """(host, path prefix) for each site: operator in a query, e.g. site:linkedin.com/in/ -> ("linkedin.com", "/in")."""
    targets = []
    for raw in _SITE.findall(text or ""):
        raw = raw.strip("\"'()").lower()
        raw = re.sub(r"^https?://", "", raw)
        host, _, path = raw.partition("/")
        host = host.removeprefix("www.")
        if host:
            targets.append((host, ("/" + path).rstrip("/") if path else ""))
    return targets


def only_sites(hits: List[Hit], targets: List[Tuple[str, str]]) -> List[Hit]:
    """Keep only results on the websites a site: operator asked for (any of them)."""
    if not targets:
        return hits

    def ok(url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        path = parts.path.lower()
        return any((host == h or host.endswith("." + h)) and (not p or path == p or path.startswith(p + "/"))
                   for h, p in targets)

    return [h for h in hits if ok(h.url)]


_COUNTRY_CODES = {
    "united states": "us", "united kingdom": "gb", "india": "in", "canada": "ca", "australia": "au",
    "germany": "de", "france": "fr", "netherlands": "nl", "spain": "es", "italy": "it", "japan": "jp",
    "brazil": "br", "mexico": "mx", "singapore": "sg", "ireland": "ie", "sweden": "se", "switzerland": "ch",
}


def _us_date(value: Optional[str]) -> str:
    """YYYY-MM-DD -> M/D/YYYY, the form Google's custom date range expects ('' = open end)."""
    if not value:
        return ""
    try:
        year, month, day = (int(part) for part in value[:10].split("-"))
    except ValueError:
        return ""
    return "%d/%d/%d" % (month, day, year)


_GOOGLE_HOST = re.compile(r"(^|\.)(google\.[a-z.]+|googleusercontent\.com|gstatic\.com)$")
_SNIPPET_XPATH = (
    ".//div[contains(@class,'VwiC3b')] | .//*[@data-sncf] | .//div[contains(@style,'-webkit-line-clamp')]"
    " | .//div[contains(@class,'BNeawe') and contains(@class,'s3v9rd')]"
)
_REL_DATE = re.compile(r"^\s*(\d+)\s+(minute|hour|day|week|month|year)s?\s+ago\s*[—–·-]\s*", re.I)
_ABS_DATE = re.compile(r"^\s*([A-Z][a-z]{2,8})\.?\s+(\d{1,2}),\s+(\d{4})\s*[—–·-]\s*")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


_GOTO = "https://www.google.com/goto?"


def _google_target(href: str) -> str:
    """Unwrap /url?q=... redirect links and drop Google's own pages.

    Some results pages hide the address behind an encrypted /goto?url=... link instead; those
    come back as an absolute https://www.google.com/goto?... link for resolve_goto_links().
    """
    if href.startswith("/goto?"):
        return _GOTO + href[len("/goto?"):]
    if href.startswith("/url?") or "google." in href and "/url?" in href:
        params = parse_qs(urlsplit(href).query)
        href = (params.get("q") or params.get("url") or [""])[0]
    if not href.startswith(("http://", "https://")):
        return ""
    host = (urlsplit(href).hostname or "").lower()
    if _GOOGLE_HOST.search(host) or host.startswith("webcache.") or host.startswith("translate."):
        return ""
    return href


def _snippet_date(snippet: str, now: Optional[float] = None) -> Tuple[str, Optional[str]]:
    """Split a leading 'Mar 5, 2026 — ' or '3 days ago — ' off a snippet; return (snippet, YYYY-MM-DD)."""
    match = _ABS_DATE.match(snippet)
    if match:
        month = _MONTHS.get(match.group(1)[:3].lower())
        if month:
            date = "%04d-%02d-%02d" % (int(match.group(3)), month, int(match.group(2)))
            return snippet[match.end():].strip(), date
    match = _REL_DATE.match(snippet)
    if match:
        seconds = {"minute": 60, "hour": 3600, "day": 86400, "week": 604800,
                   "month": 2629800, "year": 31557600}[match.group(2).lower()]
        stamp = (now or time.time()) - int(match.group(1)) * seconds
        return snippet[match.end():].strip(), time.strftime("%Y-%m-%d", time.gmtime(stamp))
    return snippet.strip(), None


def parse_google_html(markup: str) -> List[Hit]:
    """Organic results from a Google results page: every link that wraps an <h3> title."""
    tree = lxml_html.fromstring(markup)
    hits: List[Hit] = []
    seen = set()
    for link in tree.xpath("//a[.//h3]"):
        if link.xpath("ancestor::*[@id='tads' or @id='bottomads' or @data-text-ad]"):
            continue
        url = _google_target(link.get("href") or "")
        if not url or url in seen:
            continue
        title = " ".join(link.xpath(".//h3")[0].text_content().split())
        if not title:
            continue
        # Climb to the result block: the nearest ancestor that also holds the snippet.
        snippet = ""
        node = link.getparent()
        for _ in range(8):
            if node is None:
                break
            if len(node.xpath(".//h3")) > 1:
                break
            found = node.xpath(_SNIPPET_XPATH)
            if found:
                snippet = " ".join(found[0].text_content().split())
                break
            node = node.getparent()
        snippet, date = _snippet_date(snippet)
        seen.add(url)
        hits.append(Hit(title=title, url=url, snippet=snippet, published_date=date))
    return hits


def resolve_goto_links(hits: List[Hit], user_agent: str, timeout: float = 6.0) -> List[Hit]:
    """Swap encrypted Google /goto links for the real address (one free redirect lookup each).

    The SERP proxy refuses /goto, so this goes to google.com directly. Hits whose link can't be
    resolved are dropped rather than returned with a Google address.
    """
    pending = [h for h in hits if h.url.startswith(_GOTO)]
    if not pending:
        return hits

    def resolve(hit: Hit) -> Optional[str]:
        try:
            response = httpx.get(hit.url, headers={"User-Agent": user_agent}, timeout=timeout,
                                 follow_redirects=False)
        except httpx.HTTPError:
            return None
        target = _google_target(response.headers.get("location") or "")
        return target if target and not target.startswith(_GOTO) else None

    with ThreadPoolExecutor(max_workers=min(10, len(pending))) as pool:
        resolved = dict(zip((id(h) for h in pending), pool.map(resolve, pending)))
    out, seen = [], set()
    for hit in hits:
        if id(hit) in resolved:
            url = resolved[id(hit)]
            if not url:
                continue
            hit = Hit(title=hit.title, url=url, snippet=hit.snippet, published_date=hit.published_date)
        if hit.url not in seen:
            seen.add(hit.url)
            out.append(hit)
    return out


def build_engines(settings: Settings, runner: ClaudeRunner) -> list:
    engines = []
    for name in settings.backends:
        if name == "google" and settings.serp_proxy_url:
            engines.append(ApifySerpEngine(settings))
        elif name == "claude":
            engines.append(ClaudeEngine(runner, settings))
        elif name == "searxng" and settings.searxng_url:
            engines.append(SearxngEngine(settings))
        elif name == "ddg":
            engines.append(DuckDuckGoEngine(settings))
    if not engines:
        raise RuntimeError("no search engines configured (DEEPSIFT_BACKENDS)")
    return engines
