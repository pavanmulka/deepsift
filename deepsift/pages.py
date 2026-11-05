"""Fetch web pages, pull out the readable text, and rank text chunks by relevance."""
from __future__ import annotations

import io
import math
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Sequence
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura
from lxml import html as lxml_html

from .config import Settings

MAX_BYTES = 8 * 1024 * 1024
CHUNK_CHARS = 500


@dataclass
class Page:
    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""           # markdown or plain text
    published_date: Optional[str] = None
    images: List[str] = field(default_factory=list)
    favicon: Optional[str] = None
    links: List[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())


class Fetcher:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.Client(
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            limits=httpx.Limits(max_connections=settings.fetch_concurrency * 2),
        )
        self._pool = ThreadPoolExecutor(max_workers=settings.fetch_concurrency)

    def get(self, url: str, timeout: float) -> httpx.Response:
        return self._client.get(url, timeout=timeout)

    def fetch(self, url: str, *, fmt: str = "markdown", depth: str = "basic",
              timeout: float = 10.0, want_links: bool = False) -> Page:
        page = Page(url=url)
        if urlsplit(url).scheme not in ("http", "https"):
            page.error = "unsupported URL scheme"
            return page
        try:
            with self._client.stream("GET", url, timeout=timeout) as response:
                if response.status_code >= 400:
                    page.error = "HTTP %d" % response.status_code
                    return page
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        break
                page.final_url = str(response.url)
                content_type = response.headers.get("content-type", "").lower()
                encoding = response.encoding or "utf-8"
        except httpx.TimeoutException:
            page.error = "timeout"
            return page
        except httpx.HTTPError as exc:
            page.error = type(exc).__name__
            return page

        raw = bytes(body)
        if "pdf" in content_type or raw[:5] == b"%PDF-":
            page.text = _pdf_text(raw, seconds=timeout)
            page.title = page.final_url.rsplit("/", 1)[-1]
        elif "html" in content_type or "xml" in content_type or not content_type:
            _fill_from_html(page, raw.decode(encoding, errors="replace"), fmt, depth, want_links)
        elif content_type.startswith("text/"):
            page.text = raw.decode(encoding, errors="replace")
        else:
            page.error = "unsupported content type: " + content_type.split(";")[0]
            return page
        if not page.text.strip() and page.error is None:
            page.error = "no readable content"
        return page

    def fetch_many(self, urls: Sequence[str], **kwargs) -> List[Page]:
        return list(self._pool.map(lambda u: self.fetch(u, **kwargs), urls))


def _pdf_text(raw: bytes, seconds: Optional[float] = None) -> str:
    """Text of the first 200 pages; with `seconds`, stop after that long and keep what was read.

    A 300-page annual report takes minutes on a small machine (a quarter CPU core on Apify).
    """
    deadline = time.monotonic() + seconds if seconds else None
    parts: List[str] = []
    try:
        from pypdf import PdfReader
        for page in PdfReader(io.BytesIO(raw)).pages[:200]:
            parts.append(page.extract_text() or "")
            if deadline is not None and time.monotonic() > deadline:
                break
    except Exception:
        pass
    return "\n\n".join(parts).strip()


def _fill_from_html(page: Page, markup: str, fmt: str, depth: str, want_links: bool) -> None:
    base = page.final_url or page.url
    advanced = depth == "advanced"
    text = trafilatura.extract(
        markup,
        url=base,
        output_format="markdown" if fmt == "markdown" else "txt",
        include_tables=True,
        include_links=False,
        include_comments=False,
        favor_recall=advanced,
        favor_precision=not advanced,
    )
    meta = trafilatura.extract_metadata(markup, default_url=base)
    if meta is not None:
        page.title = meta.title or ""
        page.published_date = meta.date or None
    try:
        tree = lxml_html.fromstring(markup)
    except Exception:
        tree = None
    if not text and tree is not None:
        for bad in tree.xpath("//script|//style|//noscript|//nav|//footer|//header"):
            bad.drop_tree()
        text = re.sub(r"\n\s*\n+", "\n\n", tree.text_content()).strip()
    page.text = text or ""
    if tree is None:
        return
    if not page.title:
        page.title = (tree.findtext(".//title") or "").strip()
    icons = tree.xpath("//link[contains(@rel,'icon')]/@href")
    page.favicon = urljoin(base, icons[0]) if icons else urljoin(base, "/favicon.ico")
    seen = []
    for src in tree.xpath("//meta[@property='og:image']/@content | //img/@src"):
        absolute = urljoin(base, src.strip())
        if absolute.startswith("http") and absolute not in seen and not absolute.endswith(".svg"):
            seen.append(absolute)
        if len(seen) >= 8:
            break
    page.images = seen
    if want_links:
        out = []
        for href in tree.xpath("//a/@href"):
            href = href.strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            absolute = urljoin(base, href).split("#", 1)[0]
            if absolute.startswith("http") and absolute not in out:
                out.append(absolute)
        page.links = out


# ---------------------------------------------------------------- ranking

_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by for from has have how in is it its of on or that the "
    "this to was were what when where which who why will with you your".split()
)


def tokens(text: str) -> List[str]:
    return [w for w in _WORD.findall(text.lower()) if w not in _STOP]


def chunk(text: str, size: int = CHUNK_CHARS) -> List[str]:
    """Split on paragraphs, then pack paragraphs into roughly `size`-char chunks."""
    parts = [p.strip() for p in re.split(r"\n\s*\n|(?<=[.!?])\s+(?=[A-Z])", text) if p.strip()]
    out: List[str] = []
    current = ""
    for part in parts:
        while len(part) > size * 2:
            out.append(part[:size * 2])
            part = part[size * 2:]
        if current and len(current) + len(part) + 1 > size:
            out.append(current)
            current = part
        else:
            current = (current + " " + part).strip()
    if current:
        out.append(current)
    return out


def bm25(query: str, docs: Sequence[str], k1: float = 1.5, b: float = 0.75) -> List[float]:
    q = tokens(query)
    toks = [tokens(d) for d in docs]
    if not q or not docs:
        return [0.0] * len(docs)
    avg = sum(len(t) for t in toks) / len(toks) or 1.0
    df: Counter = Counter()
    for t in toks:
        df.update(set(t))
    n = len(docs)
    scores = []
    for t in toks:
        tf = Counter(t)
        s = 0.0
        for term in q:
            if term not in tf:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * len(t) / avg))
        scores.append(s)
    return scores


def best_chunks(query: str, text: str, count: int) -> List[str]:
    pieces = chunk(text)
    if not pieces:
        return []
    if not query:
        return pieces[:count]
    scores = bm25(query, pieces)
    ranked = sorted(range(len(pieces)), key=lambda i: (-scores[i], i))[:count]
    return [pieces[i] for i in sorted(ranked)]


def phrase_hits(query: str, texts: Iterable[str]) -> List[bool]:
    phrases = [p.lower() for p in re.findall(r'"([^"]+)"', query)]
    return [all(p in t.lower() for p in phrases) for t in texts]


def domain_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host[4:] if host.startswith("www.") else host


def domain_matches(url: str, domains: Iterable[str]) -> bool:
    host = domain_of(url)
    for d in domains:
        d = d.lower().strip()
        d = re.sub(r"^https?://", "", d).split("/", 1)[0]
        d = d[4:] if d.startswith("www.") else d
        if d.startswith("*."):
            d = d[2:]
        if host == d or host.endswith("." + d):
            return True
    return False


def favicon_for(url: str) -> str:
    parts = urlsplit(url)
    return "%s://%s/favicon.ico" % (parts.scheme or "https", parts.netloc)
