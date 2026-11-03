"""HTTP API with the same paths, request bodies and response bodies as api.tavily.com.

Point any Tavily client at this server by changing its base URL. Every response
reports usage.credits = 0, because nothing here is billed per call.
"""
from __future__ import annotations

import hmac
import time
import uuid
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from . import config
from .engines import EngineError
from .service import BadRequest, Service

settings = config.load()
service = Service(settings)
app = FastAPI(title="DeepSift", version="1.0")


def _error(status: int, message: str, body: Optional[Dict[str, Any]] = None) -> JSONResponse:
    payload: Dict[str, Any] = {"detail": {"error": message}, "request_id": str(uuid.uuid4())}
    if body and body.get("include_usage"):
        # Nothing is billed, so cost trackers can settle failures at 0 credits.
        payload["usage"] = {"credits": 0}
    return JSONResponse(payload, status_code=status)


def _authorized(request: Request, body: Dict[str, Any]) -> bool:
    if not settings.api_key:
        return True
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    token = token or str(body.get("api_key") or "")
    return hmac.compare_digest(token.encode(), settings.api_key.encode())


async def _body(request: Request) -> Dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        raise BadRequest("request body must be JSON")
    if not isinstance(body, dict):
        raise BadRequest("request body must be a JSON object")
    return body


async def _handle(request: Request, endpoint: str, work: Callable[[Dict[str, Any]], Dict[str, Any]],
                  status: int = 200) -> JSONResponse:
    started = time.time()
    try:
        body = await _body(request)
    except BadRequest as exc:
        return _error(400, str(exc))
    if not _authorized(request, body):
        return _error(401, "Unauthorized: missing or invalid API key.")
    try:
        result = await run_in_threadpool(work, body)
    except BadRequest as exc:
        return _error(400, str(exc), body)
    except EngineError as exc:
        return _error(502, "search engines failed: %s" % exc, body)
    service.count(endpoint)
    result = dict(result)
    result.setdefault("response_time", round(time.time() - started, 2))
    result["request_id"] = result.get("request_id") or str(uuid.uuid4())
    if body.get("include_usage"):
        result["usage"] = {"credits": 0}
    return JSONResponse(result, status_code=status)


@app.post("/search")
async def search(request: Request) -> JSONResponse:
    return await _handle(request, "search", service.search)


@app.post("/extract")
async def extract(request: Request) -> JSONResponse:
    return await _handle(request, "extract", service.extract)


@app.post("/crawl")
async def crawl(request: Request) -> JSONResponse:
    return await _handle(request, "crawl", service.crawl)


@app.post("/map")
async def map_site(request: Request) -> JSONResponse:
    return await _handle(request, "map", service.map)


@app.post("/research")
async def research(request: Request) -> JSONResponse:
    return await _handle(request, "research", service.start_research, status=201)


@app.get("/research/{request_id}")
async def research_status(request_id: str, request: Request) -> JSONResponse:
    if not _authorized(request, {}):
        return _error(401, "Unauthorized: missing or invalid API key.")
    job = service.research_status(request_id)
    if job is None:
        return _error(404, "research task not found")
    return JSONResponse(job, status_code=200 if job["status"] in ("completed", "failed") else 202)


@app.get("/usage")
async def usage(request: Request) -> JSONResponse:
    if not _authorized(request, {}):
        return _error(401, "Unauthorized: missing or invalid API key.")
    zero = {"usage": 0, "limit": None, "search_usage": 0, "extract_usage": 0,
            "crawl_usage": 0, "map_usage": 0, "research_usage": 0}
    return JSONResponse({
        "key": dict(zero),
        "account": dict(zero, current_plan="local", plan_usage=0, plan_limit=None,
                        paygo_usage=0, paygo_limit=None),
        "local": {
            "requests": service.counts,
            "claude_calls": service.claude.calls,
            "claude_web_searches": service.claude.web_searches,
            "claude_web_fetches": service.claude.web_fetches,
            "engines": [e.name for e in service.engines],
        },
    })


@app.get("/health")
async def health() -> Dict[str, Any]:
    return {"ok": True, "engines": [e.name for e in service.engines]}
