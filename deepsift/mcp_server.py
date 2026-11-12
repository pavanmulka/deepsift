"""The same search, read, crawl, map and research abilities as tools an AI app can call.

Claude Code, Claude Desktop and Codex start this file themselves (over stdio);
nothing needs to be running beforehand.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from . import config
from .engines import EngineError
from .service import BadRequest, Service

LOCAL_INSTRUCTIONS = (
    "Web research tools that run on this computer. web_search searches one topic across "
    "many websites and returns the most relevant passages from each page in one go. "
    "web_read returns the full readable text of pages you already know. web_map lists a "
    "site's pages; web_crawl reads many pages of one site. web_research runs a long "
    "multi-search investigation and returns a cited report."
)

# call(endpoint, fn, body) runs one operation and returns its result (or {"error": ...}).
Call = Callable[[str, Callable[[Dict[str, Any]], Dict[str, Any]], Dict[str, Any]], Awaitable[Dict[str, Any]]]


async def plain_call(endpoint: str, fn, body: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return await anyio.to_thread.run_sync(fn, body)
    except (BadRequest, EngineError) as exc:
        return {"error": str(exc)}


def _clean(body: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in body.items() if v not in (None, [], "")}


def _labels(title: str, *, repeat_safe: bool = True, open_web: bool = True) -> Dict[str, Any]:
    """The name people see, plus the four hints AI apps and directories read before running a tool.

    Every tool here only reads: it changes nothing on the web or in anyone's account. So
    readOnlyHint is always true and destructiveHint always false. repeat_safe=False marks a tool
    that starts a new run on every call; open_web=False marks one that never goes out to the web.
    """
    return {"title": title, "annotations": ToolAnnotations(
        title=title, read_only_hint=True, destructive_hint=False,
        idempotent_hint=repeat_safe, open_world_hint=open_web)}


def build_server(get_service: Callable[[], Service], *, call: Call = plain_call, research: bool = True,
                 instructions: str = LOCAL_INSTRUCTIONS, name: str = "deepsift") -> MCPServer:
    """The tools, bound to one Service. The hosted Apify Actor passes a charging `call` and research=False."""
    server = MCPServer(name=name, instructions=instructions)

    @server.tool(**_labels("Web Search"))
    async def web_search(
        query: str,
        max_results: int = 6,
        depth: str = "basic",
        time_range: Optional[str] = None,
        include_domains: Optional[List[str]] = None,
        exclude_domains: Optional[List[str]] = None,
        topic: str = "general",
        include_answer: bool = False,
        include_full_pages: bool = False,
    ) -> Dict[str, Any]:
        """Search the web for one topic and get back the best websites, each with the passages that answer the query.

        depth: "basic" (one passage per site), "advanced" (up to three passages per site), or "fast" (search snippets only, quickest).
        time_range: "day", "week", "month" or "year" to keep only recent pages.
        include_domains / exclude_domains: e.g. ["linkedin.com"].
        topic: "general", "news" or "finance".
        include_answer: also write a short answer from the results.
        include_full_pages: also return each page's full text (long).
        """
        body = _clean({
            "query": query, "max_results": max_results, "search_depth": depth, "time_range": time_range,
            "include_domains": include_domains, "exclude_domains": exclude_domains, "topic": topic,
            "include_answer": include_answer, "include_raw_content": include_full_pages,
            "include_published_date": True,
        })
        return await call("search", get_service().search, body)

    @server.tool(**_labels("Read Web Pages"))
    async def web_read(urls: List[str], focus: Optional[str] = None, max_passages: int = 3) -> Dict[str, Any]:
        """Read up to 20 web pages or PDFs and return their text.

        focus: if given, return only the passages most related to it (max_passages per page) instead of the whole page.
        """
        body = _clean({"urls": urls, "query": focus, "chunks_per_source": max_passages if focus else None})
        return await call("extract", get_service().extract, body)

    @server.tool(**_labels("Map a Website"))
    async def web_map(url: str, max_depth: int = 1, limit: int = 50, instructions: Optional[str] = None) -> Dict[str, Any]:
        """List the pages of a website (addresses only), following links up to max_depth clicks from the start page."""
        body = _clean({"url": url, "max_depth": max_depth, "limit": limit, "instructions": instructions,
                       "allow_external": False})
        return await call("map", get_service().map, body)

    @server.tool(**_labels("Crawl a Website"))
    async def web_crawl(url: str, instructions: Optional[str] = None, max_depth: int = 1, limit: int = 15) -> Dict[str, Any]:
        """Read many pages of one website. instructions (e.g. "pricing and plans") keeps the related pages and passages."""
        body = _clean({"url": url, "instructions": instructions, "max_depth": max_depth, "limit": limit,
                       "allow_external": False})
        return await call("crawl", get_service().crawl, body)

    if research:
        @server.tool(**_labels("Web Research Report", repeat_safe=False))
        async def web_research(question: str, thorough: bool = False, wait_minutes: float = 10) -> Dict[str, Any]:
            """Run a full investigation (many searches, many pages) and return a written report with numbered sources.

            Takes minutes. thorough=True uses a stronger model and more searches. If it is not done within
            wait_minutes, the reply carries a request_id to pass to web_research_result later.
            """
            started = await call("research", get_service().start_research, {"input": question, "model": "pro" if thorough else "mini"})
            if "error" in started:
                return started
            deadline = time.time() + wait_minutes * 60
            while time.time() < deadline:
                job = get_service().research_status(started["request_id"])
                if job and job["status"] in ("completed", "failed"):
                    return job
                await anyio.sleep(3)
            return {"request_id": started["request_id"], "status": "in_progress",
                    "note": "Still running. Call web_research_result with this request_id."}

        @server.tool(**_labels("Get Research Report", open_web=False))
        async def web_research_result(request_id: str) -> Dict[str, Any]:
            """Get the report of a web_research run that was still going."""
            return get_service().research_status(request_id) or {"error": "unknown request_id"}

    return server


_service: Optional[Service] = None


def service() -> Service:
    global _service
    if _service is None:
        _service = Service(config.load())
    return _service


server = build_server(service)


def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run("stdio")


if __name__ == "__main__":
    main()
