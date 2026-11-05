"""The Tavily-compatible operations: search, extract, crawl, map, research.

Each function takes the request body (a dict, exactly as Tavily accepts it) and
returns the response body Tavily would return. The HTTP layer adds timing,
request_id and usage.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple
from urllib import robotparser
from urllib.parse import urlsplit

from . import pages as P
from .claude_cli import ClaudeError, ClaudeRunner
from .config import Settings
from .engines import EngineError, Hit, Query, build_engines
from .rerank import Reranker, clean_question, to_unit
from .store import Store


class BadRequest(ValueError):
    pass


def _bool(body: Dict[str, Any], name: str, default: bool = False) -> bool:
    value = body.get(name, default)
    if isinstance(value, str):
        return value.lower() not in ("", "false", "0", "no")
    return bool(value)


def _int(body: Dict[str, Any], name: str, default: int, lo: int, hi: int) -> int:
    value = body.get(name, default)
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise BadRequest("%s must be an integer" % name)
    if not lo <= value <= hi:
        raise BadRequest("%s must be between %d and %d" % (name, lo, hi))
    return value


def _strings(body: Dict[str, Any], name: str) -> List[str]:
    value = body.get(name) or []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadRequest("%s must be a list of strings" % name)
    return [v for v in value if v.strip()]


class Service:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.claude = ClaudeRunner(settings)
        self.fetcher = P.Fetcher(settings)
        self.store = Store(settings.data_dir / "deepsift.sqlite3")
        self.engines = build_engines(settings, self.claude)
        self.reranker = Reranker(settings.reranker, settings.reranker_cache or None) if settings.reranker else None
        self.counts: Dict[str, int] = {}
        self._count_lock = threading.Lock()

    def count(self, endpoint: str) -> None:
        with self._count_lock:
            self.counts[endpoint] = self.counts.get(endpoint, 0) + 1

    # ------------------------------------------------------------ search

    def search(self, body: Dict[str, Any]) -> Dict[str, Any]:
        query = str(body.get("query") or "").strip()
        if not query:
            raise BadRequest("query is required")
        depth = body.get("search_depth") or "basic"
        if depth not in ("basic", "advanced", "fast", "ultra-fast"):
            raise BadRequest("search_depth must be basic, advanced, fast or ultra-fast")
        topic = body.get("topic") or "general"
        if topic not in ("general", "news", "finance"):
            raise BadRequest("topic must be general, news or finance")
        max_results = _int(body, "max_results", 10, 0, 20)
        chunks = _int(body, "chunks_per_source", 3, 1, 3)
        include_domains = _strings(body, "include_domains")
        exclude_domains = _strings(body, "exclude_domains")
        domains_mode = body.get("include_domains_mode") or "restrict"
        if body.get("include_domains_mode") and not include_domains:
            raise BadRequest("include_domains_mode requires include_domains")
        if _bool(body, "filter_by_language") and not body.get("language"):
            raise BadRequest("filter_by_language requires language")
        raw_mode = body.get("include_raw_content", False)
        raw_fmt = "text" if raw_mode == "text" else ("markdown" if raw_mode else None)
        answer_mode = body.get("include_answer", False)
        answer_mode = "basic" if answer_mode is True else (answer_mode or None)
        include_images = _bool(body, "include_images")
        include_favicon = _bool(body, "include_favicon")
        include_date = _bool(body, "include_published_date") or topic == "news" or _bool(body, "filter_by_published_date")

        max_age = body.get("max_age_hours")
        max_age = self.settings.cache_hours if max_age is None else float(max_age)
        cache_key = self.store.key("search", {k: v for k, v in body.items()
                                              if k not in ("api_key", "include_usage", "max_age_hours")})
        cached = self.store.cached(cache_key, max_age)
        if cached is not None:
            return cached
        if max_age == -1:
            max_age = self.settings.cache_hours

        q = Query(
            text=query,
            max_results=max(max_results, 5) + 3,  # head-room for filtering
            topic=topic,
            time_range=body.get("time_range"),
            start_date=body.get("start_date"),
            end_date=body.get("end_date"),
            include_domains=include_domains if domains_mode == "restrict" else None,
            exclude_domains=exclude_domains,
            country=body.get("country"),
            language=body.get("language"),
            safe_search=_bool(body, "safe_search"),
        )
        hits, engine = self._run_engines(q)
        hits = [h for h in hits if not (exclude_domains and P.domain_matches(h.url, exclude_domains))]
        if include_domains and domains_mode == "restrict":
            hits = [h for h in hits if P.domain_matches(h.url, include_domains)]
        elif include_domains:
            hits.sort(key=lambda h: not P.domain_matches(h.url, include_domains))

        fetch_pages = depth in ("basic", "advanced") or raw_fmt or include_images or include_date
        page_by_url: Dict[str, P.Page] = {}
        if fetch_pages and hits:
            fetched = self.fetcher.fetch_many(
                [h.url for h in hits],
                fmt=raw_fmt or "markdown",
                depth="advanced" if depth == "advanced" else "basic",
                timeout=12.0 if depth == "advanced" else 7.0,
            )
            page_by_url = {p.url: p for p in fetched}

        ai = self._ai_passages(query, hits, page_by_url, depth, chunks) if self.reranker else {}
        results = []
        for rank, hit in enumerate(hits):
            page = page_by_url.get(hit.url)
            content = hit.snippet
            if hit.url in ai:
                content = ai[hit.url][1]
            elif page is not None and page.ok and depth in ("basic", "advanced"):
                best = P.best_chunks(query, page.text, chunks if depth == "advanced" else 1)
                if best:
                    content = " [...] ".join(best)
                    if depth == "basic" and hit.snippet and hit.snippet not in content:
                        content = hit.snippet.rstrip() + " " + content
            row: Dict[str, Any] = {
                "title": hit.title or (page.title if page else ""),
                "url": hit.url,
                "content": content,
                "score": 0.0,
                "_rank": rank,
                "_ai": ai[hit.url][0] if hit.url in ai else None,
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, hit.url)),
            }
            if raw_fmt:
                row["raw_content"] = page.text if page is not None and page.ok else None
            if include_date:
                row["published_date"] = _iso_date(hit.published_date or (page.published_date if page else None))
            if include_favicon:
                row["favicon"] = (page.favicon if page and page.favicon else P.favicon_for(hit.url))
            if include_images:
                row["images"] = [{"url": u} for u in (page.images[:3] if page else [])]
            results.append(row)

        results = self._filter_and_score(results, query, body)
        results = results[:max_results]
        for row in results:
            row.pop("_rank", None)
            row.pop("_ai", None)

        response: Dict[str, Any] = {"query": query, "follow_up_questions": None, "answer": None,
                                    "images": [], "results": results}
        if include_images:
            images = []
            for row in results:
                images.extend(row.get("images", []))
            response["images"] = images[:10]
        if answer_mode and results:
            response["answer"] = self._answer(query, results, answer_mode)
        response["engine"] = engine
        self.store.put(cache_key, response)
        return response

    def _run_engines(self, q: Query) -> Tuple[List[Hit], str]:
        if self.settings.merge_engines and len(self.engines) > 1:
            hits, name = self._merge_engines(q)
            loose = _loosen(q.text)
            if len(hits) * 2 < q.max_results and loose != q.text:
                # Exact phrases like "Acme (Pittsburgh, PA)" match almost nothing; look once more without quotes.
                try:
                    more, _ = self._merge_engines(replace(q, text=loose))
                except EngineError:
                    more = []
                known = {_url_key(h.url) for h in hits}
                hits = hits + [h for h in more if _url_key(h.url) not in known]
            return hits[:q.max_results], name
        errors = []
        for engine in self.engines:
            try:
                hits = engine.search(q)
            except EngineError as exc:
                errors.append(str(exc))
                continue
            if hits:
                return hits, engine.name
            errors.append("%s: no results" % engine.name)
        if errors and all("no results" in e for e in errors):
            return [], self.engines[-1].name
        raise EngineError("; ".join(errors))

    def _merge_engines(self, q: Query) -> Tuple[List[Hit], str]:
        """Ask every engine at once; rank by reciprocal rank fusion so links several engines agree on come first."""
        def run(engine: Any) -> Tuple[Any, List[Hit], Optional[str]]:
            try:
                return engine, engine.search(q), None
            except EngineError as exc:
                return engine, [], str(exc)

        with ThreadPoolExecutor(max_workers=len(self.engines)) as pool:
            answers = list(pool.map(run, self.engines))
        scores: Dict[str, float] = {}
        best: Dict[str, Hit] = {}
        order: Dict[str, int] = {}
        used = []
        for position, (engine, hits, _) in enumerate(answers):
            if hits:
                used.append(engine.name)
            for rank, hit in enumerate(hits):
                key = _url_key(hit.url)
                scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
                if key not in best:
                    best[key] = hit
                    order[key] = position * 1000 + rank
                elif not best[key].snippet and hit.snippet:
                    best[key] = Hit(title=best[key].title or hit.title, url=best[key].url, snippet=hit.snippet,
                                    published_date=best[key].published_date or hit.published_date)
                elif not best[key].published_date and hit.published_date:
                    best[key].published_date = hit.published_date
        if not best:
            errors = [err for _, _, err in answers if err]
            if len(errors) == len(answers):  # an engine that answered "nothing found" is an answer
                raise EngineError("; ".join(errors))
            return [], "+".join(e.name for e, _, err in answers if not err)
        keys = sorted(best, key=lambda k: (-scores[k], order[k]))[:q.max_results]  # only what we'll read
        return [best[k] for k in keys], "+".join(used)

    def _ai_passages(self, query: str, hits: List[Hit], page_by_url: Dict[str, "P.Page"], depth: str,
                     chunks: int) -> Dict[str, Tuple[float, str]]:
        """AI-score each result's best candidate passages: url -> (best raw score, content to return)."""
        question = clean_question(query) or query
        jobs: List[Tuple[str, str, str]] = []  # (url, passage, text the scorer reads)
        for hit in hits:
            page = page_by_url.get(hit.url)
            candidates = []
            if page is not None and page.ok and depth in ("basic", "advanced"):
                candidates = P.best_chunks(question, page.text, 3)
            if hit.snippet:
                candidates.append(hit.snippet)
            if not candidates:
                candidates = [hit.title]
            for passage in candidates:
                jobs.append((hit.url, passage, (hit.title + ". " + passage).strip()))
        try:
            raw = self.reranker.scores(question, [j[2] for j in jobs])
        except Exception:  # a missing or broken model must never break search
            return {}
        by_url: Dict[str, List[Tuple[float, int, str]]] = {}
        for i, ((url, passage, _), score) in enumerate(zip(jobs, raw)):
            by_url.setdefault(url, []).append((score, i, passage))
        out: Dict[str, Tuple[float, str]] = {}
        keep = chunks if depth == "advanced" else 1
        for url, scored in by_url.items():
            top = sorted(scored, key=lambda t: -t[0])[:keep]
            content = " [...] ".join(p for _, _, p in sorted(top, key=lambda t: t[1]))
            out[url] = (top[0][0], content)
        return out

    def _filter_and_score(self, results: List[Dict[str, Any]], query: str, body: Dict[str, Any]) -> List[Dict[str, Any]]:
        if _bool(body, "exact_match") and '"' in query:
            texts = [r["title"] + " " + r["content"] + " " + (r.get("raw_content") or "") for r in results]
            results = [r for r, ok in zip(results, P.phrase_hits(query, texts)) if ok]
        if _bool(body, "filter_by_published_date"):
            lo, hi = _date_window(body)
            kept = []
            for r in results:
                d = r.get("published_date")
                if d and (lo is None or d[:10] >= lo) and (hi is None or d[:10] <= hi):
                    kept.append(r)
            results = kept
        if not results:
            return results
        n = len(results)
        if any(r.get("_ai") is not None for r in results):
            # The AI scorer decides; the engines' own order only breaks near-ties.
            for r in results:
                position = 1.0 - r["_rank"] / max(n, 1)
                ai = to_unit(r["_ai"]) if r.get("_ai") is not None else 0.0
                r["score"] = round(0.85 * ai + 0.15 * position, 4)
            results.sort(key=lambda r: -r["score"])
            return results
        relevance = P.bm25(query, [r["title"] + " " + r["content"] for r in results])
        top = max(relevance) or 1.0
        for r, rel in zip(results, relevance):
            position = 1.0 - r["_rank"] / max(n, 1)
            r["score"] = round(0.35 + 0.4 * position + 0.25 * (rel / top), 4)
        results.sort(key=lambda r: -r["score"])
        return results

    def _answer(self, query: str, results: List[Dict[str, Any]], mode: str) -> Optional[str]:
        if not self.settings.llm_enabled:
            return None
        sources = "\n\n".join(
            "[%d] %s (%s)\n%s" % (i + 1, r["title"], r["url"], (r.get("raw_content") or r["content"])[:3000])
            for i, r in enumerate(results)
        )
        length = "a detailed, well-structured answer (2-4 paragraphs)" if mode == "advanced" else "a short answer (1-3 sentences)"
        prompt = (
            "Question: %s\n\nUsing ONLY the sources below, write %s. If the sources do not "
            "answer the question, say so briefly. Plain text, no markdown headings, no "
            "citation markers.\n\nSources:\n%s" % (query, length, sources)
        )
        model = self.settings.advanced_model if mode == "advanced" else self.settings.answer_model
        try:
            return self.claude.run(prompt, model=model) or None
        except ClaudeError:
            return None

    # ------------------------------------------------------------ extract

    def extract(self, body: Dict[str, Any]) -> Dict[str, Any]:
        urls = body.get("urls")
        if isinstance(urls, str):
            urls = [urls]
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
            raise BadRequest("urls must be a URL or a list of 1-20 URLs")
        if len(urls) > 20:
            raise BadRequest("at most 20 urls per request")
        depth = body.get("extract_depth") or "basic"
        if depth not in ("basic", "advanced"):
            raise BadRequest("extract_depth must be basic or advanced")
        fmt = body.get("format") or "markdown"
        if fmt not in ("markdown", "text"):
            raise BadRequest("format must be markdown or text")
        query = str(body.get("query") or "").strip()
        chunks = _int(body, "chunks_per_source", 3, 1, 5)
        timeout = float(body.get("timeout") or (30.0 if depth == "advanced" else 10.0))
        timeout = min(max(timeout, 1.0), 60.0)

        fetched = self.fetcher.fetch_many(urls, fmt=fmt, depth=depth, timeout=timeout)
        results, failed = [], []
        for page in fetched:
            if not page.ok:
                failed.append({"url": page.url, "error": page.error or "no readable content"})
                continue
            text = page.text
            if query:
                text = " [...] ".join(P.best_chunks(query, text, chunks))
            row: Dict[str, Any] = {"url": page.url, "raw_content": text}
            if _bool(body, "include_images"):
                row["images"] = page.images
            if _bool(body, "include_favicon"):
                row["favicon"] = page.favicon or P.favicon_for(page.url)
            results.append(row)
        return {"results": results, "failed_results": failed}

    # ------------------------------------------------------------ crawl / map

    def crawl(self, body: Dict[str, Any], extract: bool = True) -> Dict[str, Any]:
        root = str(body.get("url") or "").strip()
        if not root:
            raise BadRequest("url is required")
        if "://" not in root:
            root = "https://" + root
        max_depth = _int(body, "max_depth", 1, 1, 5)
        max_breadth = _int(body, "max_breadth", 20, 1, 500)
        limit = _int(body, "limit", 50, 1, 10000)
        timeout = min(max(float(body.get("timeout") or 150), 10.0), 150.0)
        instructions = str(body.get("instructions") or "").strip()
        chunks = _int(body, "chunks_per_source", 3, 1, 5)
        fmt = body.get("format") or "markdown"
        depth = body.get("extract_depth") or "basic"
        allow_external = _bool(body, "allow_external", True)
        select_paths = [re.compile(p) for p in _strings(body, "select_paths")]
        exclude_paths = [re.compile(p) for p in _strings(body, "exclude_paths")]
        select_domains = [re.compile(p) for p in _strings(body, "select_domains")]
        exclude_domains = [re.compile(p) for p in _strings(body, "exclude_domains")]
        root_host = P.domain_of(root)
        deadline = time.time() + timeout
        robots = _Robots(self.fetcher, self.settings.user_agent)

        def in_scope(url: str) -> bool:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            path = parts.path or "/"
            if select_domains:
                if not any(r.search(host) for r in select_domains):
                    return False
            elif P.domain_of(url) != root_host and not P.domain_of(url).endswith("." + root_host):
                return False
            if any(r.search(host) for r in exclude_domains):
                return False
            if select_paths and not any(r.search(path) for r in select_paths):
                return False
            if any(r.search(path) for r in exclude_paths):
                return False
            return True

        seen = {_canonical(root)}
        found: List[str] = []
        listed: List[str] = []   # map only: leaf URLs listed without fetching
        external: List[str] = []
        pages_out: List[P.Page] = []
        frontier = deque([(root, 0)])
        while frontier and len(found) + len(listed) < limit and time.time() < deadline:
            batch = []
            while frontier and len(batch) < self.settings.fetch_concurrency and len(found) + len(listed) + len(batch) < limit:
                url, level = frontier.popleft()
                if not extract and level >= max_depth and level > 0:
                    listed.append(url)   # a map has no use for a leaf page's text
                    continue
                batch.append((url, level))
            batch = [(u, d) for u, d in batch if robots.allowed(u)]
            if not batch:
                continue
            fetched = self.fetcher.fetch_many(
                [u for u, _ in batch], fmt=fmt, depth=depth,
                timeout=min(15.0, max(1.0, deadline - time.time())), want_links=True,
            )
            for (url, level), page in zip(batch, fetched):
                if page.error:
                    continue
                found.append(url)
                pages_out.append(page)
                if level >= max_depth:
                    continue
                inside = []
                for link in page.links:
                    key = _canonical(link)
                    if key in seen:
                        continue
                    seen.add(key)
                    if in_scope(link):
                        inside.append(link)
                    elif allow_external and link not in external:
                        external.append(link)
                if instructions and inside:
                    # Follow the links that look most related to the instructions first.
                    scores = P.bm25(instructions, [re.sub(r"[/_.-]", " ", urlsplit(u).path) for u in inside])
                    inside = [u for _, u in sorted(zip(scores, inside), key=lambda pair: -pair[0])]
                for link in inside[:max_breadth]:
                    frontier.append((link, level + 1))

        if instructions:
            scores = P.bm25(instructions, [p.url + " " + p.title + " " + p.text[:4000] for p in pages_out])
            order = sorted(range(len(pages_out)), key=lambda i: -scores[i])
            pages_out = [pages_out[i] for i in order if scores[i] > 0 or i == 0]

        if not extract:
            urls = [p.url for p in pages_out] + listed
            if allow_external:
                urls += [u for u in external if u not in urls][: max(0, limit - len(urls))]
            return {"base_url": root, "results": urls[:limit]}

        results = []
        for page in pages_out:
            text = page.text
            if instructions:
                text = " [...] ".join(P.best_chunks(instructions, text, chunks))
            row: Dict[str, Any] = {"url": page.url, "raw_content": text}
            if _bool(body, "include_images"):
                row["images"] = page.images
            if _bool(body, "include_favicon"):
                row["favicon"] = page.favicon or P.favicon_for(page.url)
            results.append(row)
        return {"base_url": root, "results": results}

    def map(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self.crawl(body, extract=False)

    # ------------------------------------------------------------ research

    def start_research(self, body: Dict[str, Any]) -> Dict[str, Any]:
        if not self.settings.llm_enabled:
            raise BadRequest("research is not available on this server")
        task = str(body.get("input") or "").strip()
        if not task:
            raise BadRequest("input is required")
        model = body.get("model") or "auto"
        if model not in ("mini", "pro", "auto"):
            raise BadRequest("model must be mini, pro or auto")
        if _bool(body, "stream"):
            raise BadRequest("stream=true is not supported; poll GET /research/{request_id}")
        job = {
            "request_id": str(uuid.uuid4()),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "status": "pending",
            "input": task,
            "model": model,
        }
        self.store.save_job(job)
        threading.Thread(target=self._research, args=(job, body), daemon=True).start()
        return dict(job)

    def research_status(self, request_id: str) -> Optional[Dict[str, Any]]:
        return self.store.job(request_id)

    def _research(self, job: Dict[str, Any], body: Dict[str, Any]) -> None:
        started = time.time()
        job = dict(job, status="in_progress")
        self.store.save_job(job)
        model_name = job["model"]
        pro = model_name == "pro" or (model_name == "auto" and len(job["input"]) > 200)
        length = {"short": "about 300 words", "long": "1500-2500 words"}.get(
            body.get("output_length") or "standard", "700-1000 words")
        citation = body.get("citation_format") or "numbered"
        rules = []
        include = _strings(body, "include_domains")
        exclude = _strings(body, "exclude_domains")
        if include:
            rules.append("Prefer sources from: " + ", ".join(include) + ".")
        if exclude:
            rules.append("Never use sources from: " + ", ".join(exclude) + ".")
        files = body.get("files") or []
        attached = ""
        for f in files[:5]:
            try:
                import base64
                attached += "\n\n--- %s ---\n%s" % (f.get("name"), base64.b64decode(f.get("data", "")).decode("utf-8", "replace")[:200000])
            except Exception:
                continue
        schema = body.get("output_schema")
        if schema:
            out_schema = {
                "type": "object",
                "properties": {
                    "content": schema,
                    "sources": {"type": "array", "items": {"type": "object", "properties": {
                        "title": {"type": "string"}, "url": {"type": "string"}}, "required": ["url"]}},
                },
                "required": ["content", "sources"],
            }
        else:
            out_schema = {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "sources": {"type": "array", "items": {"type": "object", "properties": {
                        "title": {"type": "string"}, "url": {"type": "string"}}, "required": ["url"]}},
                },
                "required": ["content", "sources"],
            }
        prompt = (
            "Research task:\n%s\n\nInvestigate with WebSearch and WebFetch (%s). %s\n"
            "Then return `content`: %s, written in markdown, with %s citations that point to "
            "entries in `sources`. Every claim must come from a page you actually opened or "
            "a search result you saw; say plainly when something could not be verified. "
            "`sources` lists every cited page (title, url) in citation order.%s"
        ) % (
            job["input"],
            "be thorough: 8-20 searches and open the most important pages" if pro else "5-10 searches",
            " ".join(rules),
            "an object matching the given schema" if schema else "a research report of " + length,
            citation,
            ("\n\nAttached files:" + attached) if attached else "",
        )
        try:
            data = self.claude.run(
                prompt,
                model=self.settings.research_model if pro else self.settings.advanced_model,
                tools=["WebSearch", "WebFetch"],
                schema=out_schema,
                timeout=1800 if pro else 900,
            )
            sources = []
            for s in (data.get("sources") or []):
                if isinstance(s, dict) and str(s.get("url", "")).startswith("http"):
                    sources.append({"title": s.get("title") or "", "url": s["url"],
                                    "favicon": P.favicon_for(s["url"])})
            job.update(status="completed", content=data.get("content"), sources=sources)
        except (ClaudeError, AttributeError) as exc:
            job.update(status="failed", error=str(exc))
        job["response_time"] = round(time.time() - started, 2)
        job["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        self.store.save_job(job)


class _Robots:
    def __init__(self, fetcher: P.Fetcher, agent: str) -> None:
        self._fetcher = fetcher
        self._agent = agent
        self._parsers: Dict[str, Optional[robotparser.RobotFileParser]] = {}

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = "%s://%s" % (parts.scheme, parts.netloc)
        if origin not in self._parsers:
            parser: Optional[robotparser.RobotFileParser] = robotparser.RobotFileParser()
            try:
                response = self._fetcher.get(origin + "/robots.txt", timeout=5)
                if response.status_code >= 400:
                    parser = None
                else:
                    parser.parse(response.text.splitlines())
            except Exception:
                parser = None
            self._parsers[origin] = parser
        parser = self._parsers[origin]
        return True if parser is None else parser.can_fetch(self._agent, url)


def _canonical(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or ""
    return "%s://%s%s%s" % (parts.scheme, (parts.netloc or "").lower(), path,
                            "?" + parts.query if parts.query else "")


def _iso_date(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2})", str(value))
    return match.group(1) if match else str(value)


def _date_window(body: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    lo, hi = body.get("start_date"), body.get("end_date")
    tr = body.get("time_range")
    days = {"day": 1, "d": 1, "week": 7, "w": 7, "month": 31, "m": 31, "year": 366, "y": 366}.get(tr or "")
    if days and not lo:
        lo = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    return lo, hi


def _url_key(url: str) -> str:
    """The same page as different engines write it: no scheme, no www., no trailing slash or fragment."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.")
    return host + parts.path.rstrip("/") + ("?" + parts.query if parts.query else "")


def _loosen(text: str) -> str:
    """Drop quotes and brackets but keep site: and the words: '"Acme (Ayer, MA)" CFO' -> 'Acme Ayer, MA CFO'."""
    return " ".join(re.sub(r'["()]', " ", text).split())
