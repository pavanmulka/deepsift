"""DeepSift as a paid Apify Actor.

Two ways to use it:
- Standby (always-on web address): Tavily's /search, /extract, /crawl and /map, plus an MCP
  server at /mcp for Claude, Cursor and other AI apps. Apify checks the caller's Apify token.
- Normal run: a list of search queries in the input, one dataset row per search result.

Search asks Google (through Apify's GOOGLE_SERP proxy) and Yahoo (plain requests) at the same
time and merges the two. Nothing here calls `claude -p`: a hosted copy
must never run on the owner's personal Claude subscription, so written answers and research
reports are switched off.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

import anyio
from apify import Actor

from . import billing, config
from .api import create_app
from .engines import EngineError
from .mcp_server import build_server
from .service import BadRequest, Service

INSTRUCTIONS = (
    "Live web search for AI agents. web_search searches Google and Yahoo for one topic and returns the most "
    "relevant passages from each result page in one go. web_read returns the readable text of pages "
    "you already know. web_map lists a site's pages; web_crawl reads many pages of one site."
)
BUDGET_MESSAGE = "The spending limit for this run is used up. Raise the maximum charge or start a new run."


async def build_service() -> Service:
    os.environ.setdefault("DEEPSIFT_DATA_DIR", "/tmp/deepsift")
    os.environ["DEEPSIFT_LLM"] = "0"
    settings = config.load()
    proxy = await Actor.create_proxy_configuration(groups=["GOOGLE_SERP"])
    proxy_url = await proxy.new_url() if proxy else None
    if not proxy_url:
        raise RuntimeError("Apify GOOGLE_SERP proxy is not available (run on Apify, or `apify login` first)")
    # Google and Yahoo at once, merged; a search fails only when both fail. The second round without
    # quotes (for searches that found too little) asks Yahoo only, so it never buys a Google page.
    settings = dataclasses.replace(settings, backends=["google", "yahoo"], merge_engines=True,
                                   loose_backends=["yahoo"], serp_proxy_url=proxy_url,
                                   api_key="", llm_enabled=False)
    service = Service(settings)
    if service.reranker is not None:
        # Load the AI scorer while the first Google page is on its way, not after it.
        threading.Thread(target=service.reranker.warm, daemon=True).start()
    return service


def budget_left(events: List[str]) -> bool:
    manager = Actor.get_charging_manager()
    return not any(manager.is_event_charge_limit_reached(e) for e in events)


async def charge(endpoint: str, body: Dict[str, Any], result: Dict[str, Any]) -> None:
    for event, count in billing.events_for(endpoint, body, result):
        if count > 0:
            await Actor.charge(event_name=event, count=count)


async def refuse_when_over_budget(endpoint: str, body: Dict[str, Any]) -> Optional[str]:
    return None if budget_left(list(billing.ALL_EVENTS)) else BUDGET_MESSAGE


async def charged_call(endpoint: str, fn, body: Dict[str, Any]) -> Dict[str, Any]:
    if not budget_left(list(billing.ALL_EVENTS)):
        return {"error": BUDGET_MESSAGE}
    try:
        result = await anyio.to_thread.run_sync(fn, body)
    except (BadRequest, EngineError) as exc:
        return {"error": str(exc)}
    await charge(endpoint, body, result)
    return result


def build_web_app(service: Service):
    """One web server: the MCP endpoint at /mcp, everything else goes to the Tavily-style API."""
    from mcp.server.transport_security import TransportSecuritySettings
    from starlette.responses import JSONResponse

    api = create_app(service, service.settings, before=refuse_when_over_budget, after=charge,
                     title="DeepSift Search")

    @api.get("/")
    async def root() -> JSONResponse:
        # Apify's readiness probe and a friendly answer for people who open the URL.
        return JSONResponse({
            "name": "DeepSift Search",
            "endpoints": ["POST /search", "POST /extract", "POST /crawl", "POST /map", "MCP /mcp"],
            "docs": "Same request and response format as api.tavily.com. Send your Apify token "
                    "as 'Authorization: Bearer <token>'.",
        })

    mcp = build_server(lambda: service, call=charged_call, research=False,
                       instructions=INSTRUCTIONS, name="deepsift-search")
    web = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        # Apify's own proxy sits in front and checks the token; requests arrive with Apify's host name.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    web.mount("/", api)
    return web


async def run_standby(service: Service) -> None:
    import uvicorn

    port = int(os.environ.get("ACTOR_WEB_SERVER_PORT") or Actor.configuration.web_server_port or 4321)
    Actor.log.info("Standby web server on port %s", port)
    server = uvicorn.Server(uvicorn.Config(build_web_app(service), host="0.0.0.0", port=port,
                                           log_level="warning"))
    await server.serve()


def search_body(actor_input: Dict[str, Any], query: str) -> Dict[str, Any]:
    """Normal-run input (camelCase, as Apify Console shows it) -> a Tavily search body."""
    body: Dict[str, Any] = {
        "query": query,
        "search_depth": actor_input.get("searchDepth") or "basic",
        "max_results": int(actor_input.get("maxResults") or 5),
        "topic": actor_input.get("topic") or "general",
        "include_published_date": True,
    }
    optional = {
        "time_range": actor_input.get("timeRange"),
        "include_domains": actor_input.get("includeDomains"),
        "exclude_domains": actor_input.get("excludeDomains"),
        "country": actor_input.get("country"),
        "language": actor_input.get("language"),
        "include_raw_content": "markdown" if actor_input.get("includeFullPages") else None,
    }
    body.update({k: v for k, v in optional.items() if v not in (None, "", [])})
    return body


def result_rows(query: str, response: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = []
    for rank, result in enumerate(response.get("results") or [], start=1):
        row = {"query": query, "rank": rank, "title": result.get("title"), "url": result.get("url"),
               "content": result.get("content"), "score": result.get("score"),
               "publishedDate": result.get("published_date")}
        if "raw_content" in result:
            row["fullPage"] = result.get("raw_content")
        rows.append(row)
    return rows


async def save_empty_page(service: Service, query: str) -> None:
    """Keep Google's page for a search that found nothing, so the page reader can be fixed."""
    for engine in service.engines:
        html = getattr(engine, "last_html", "")
        if html:
            await Actor.set_value("EMPTY_GOOGLE_PAGE", html, content_type="text/html")
            Actor.log.warning("No results for %r; saved Google's page as EMPTY_GOOGLE_PAGE", query)


def google_pages(service: Service) -> int:
    return sum(engine.pages_fetched for engine in service.engines if engine.name == "google")


async def run_batch(service: Service) -> None:
    actor_input = await Actor.get_input() or {}
    queries = [q.strip() for q in actor_input.get("queries") or [] if isinstance(q, str) and q.strip()]
    if not queries:
        await Actor.fail(status_message="Add at least one search query.")
        return
    done = 0
    for query in queries:
        body = search_body(actor_input, query)
        event = billing.events_for("search", body, {})[0][0]
        if not budget_left([event]):
            Actor.log.info("Spending limit reached after %d searches", done)
            break
        started, pages_before = time.monotonic(), google_pages(service)
        try:
            response = await anyio.to_thread.run_sync(service.search, body)
        except BadRequest as exc:
            await Actor.fail(status_message="Bad input: %s" % exc)
            return
        except EngineError as exc:
            Actor.log.warning("Search failed for %r: %s", query, exc)
            await Actor.push_data({"query": query, "error": str(exc)})
            continue
        await Actor.charge(event_name=event)
        rows = result_rows(query, response)
        Actor.log.info("Search %d of %d: %d results in %.1f s from %s, %d Google page(s)", done + 1,
                       len(queries), len(rows), time.monotonic() - started, response.get("engine") or "-",
                       google_pages(service) - pages_before)
        if not rows:
            await save_empty_page(service, query)
        await Actor.push_data(rows or [{"query": query, "error": "no results"}])
        done += 1
    await Actor.set_status_message("Finished %d of %d searches" % (done, len(queries)))


async def main() -> None:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    async with Actor:
        service = await build_service()
        if os.environ.get("APIFY_META_ORIGIN") == "STANDBY":
            await run_standby(service)
        else:
            await run_batch(service)


if __name__ == "__main__":
    asyncio.run(main())
