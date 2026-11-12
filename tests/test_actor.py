"""Offline tests for the hosted Apify Actor: Google parsing, charging and the web server."""
import dataclasses
import json
import pathlib

import httpx
import pytest
from fastapi.testclient import TestClient

from deepsift import billing, config
from deepsift import engines as E
from deepsift import pages as P
from deepsift.service import Service

HERE = pathlib.Path(__file__).parent
SAMPLE = (HERE / "google_sample.html").read_text()


def test_parse_google_html_keeps_organic_results_only():
    hits = E.parse_google_html(SAMPLE)
    assert [h.url for h in hits] == [
        "https://qdrant.tech/benchmarks/",
        "https://milvus.io/docs/overview.md",   # /url?q= redirect unwrapped
        "https://weaviate.io/blog/vector-search",
    ]  # ads, Google's own links and the duplicate are dropped
    assert hits[0].title == "Vector Database Benchmarks - Qdrant"
    assert hits[0].snippet == "Qdrant compares vector databases on recall and latency."
    assert hits[0].published_date == "2026-03-05"
    assert hits[1].snippet.startswith("Milvus is an open-source")
    assert hits[1].published_date  # "3 days ago" becomes a date
    assert hits[2].published_date is None


class FakeGoogle:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def __call__(self, url, params=None, proxy=None, **kw):
        self.calls.append({"url": url, "params": dict(params or {}), "proxy": proxy})
        return httpx.Response(200, text=self.pages.pop(0), request=httpx.Request("GET", url))


def _settings(tmp_path):
    base = config.Settings(data_dir=tmp_path)
    return dataclasses.replace(base, backends=["google"], serp_proxy_url="http://groups-GOOGLE_SERP:pw@proxy:8000",
                               llm_enabled=False, cache_hours=0)


def test_serp_engine_builds_google_request(tmp_path, monkeypatch):
    fake = FakeGoogle([SAMPLE])
    monkeypatch.setattr(E.httpx, "get", fake)
    engine = E.ApifySerpEngine(_settings(tmp_path))
    q = E.Query(text="vector databases", max_results=8, topic="news", time_range="week",
                include_domains=["qdrant.tech"], exclude_domains=["spam.com"], country="india", language="en")
    hits = engine.search(q)
    assert len(hits) == 3 and engine.pages_fetched == 1
    call = fake.calls[0]
    assert call["url"] == "http://www.google.com/search"  # the SERP proxy only takes plain HTTP
    assert call["proxy"].startswith("http://groups-GOOGLE_SERP:")
    p = call["params"]
    assert p["q"] == "vector databases (site:qdrant.tech) -site:spam.com"
    assert p["tbs"] == "qdr:w" and p["tbm"] == "nws" and p["gl"] == "in" and p["hl"] == "en"
    assert "start" not in p


def test_serp_engine_reads_second_page_only_when_asked(tmp_path, monkeypatch):
    page = SAMPLE.replace("</div></div>\n<div id=\"bottomads\">",
                          "".join('<div class="g"><a href="https://site%d.com/"><h3>Site %d</h3></a></div>' % (i, i)
                                  for i in range(5)) + "</div></div>\n<div id=\"bottomads\">")
    fake = FakeGoogle([page, SAMPLE])
    monkeypatch.setattr(E.httpx, "get", fake)
    engine = E.ApifySerpEngine(_settings(tmp_path))
    engine.search(E.Query(text="x", max_results=13))   # caller asked for 10 (+3 head-room)
    assert len(fake.calls) == 1
    fake.pages = [page, SAMPLE]
    engine.search(E.Query(text="x", max_results=18))   # caller asked for 15
    assert [c["params"].get("start") for c in fake.calls[1:]] == [None, "10"]


def test_custom_date_range_uses_google_format(tmp_path):
    engine = E.ApifySerpEngine(_settings(tmp_path))
    p = engine._params(E.Query(text="x", start_date="2026-01-02", end_date="2026-03-04"), 0)
    assert p["tbs"] == "cdr:1,cd_min:1/2/2026,cd_max:3/4/2026"


def test_billing_events():
    assert billing.events_for("search", {"search_depth": "advanced"}, {}) == [(billing.ADVANCED_SEARCH, 1)]
    assert billing.events_for("search", {}, {}) == [(billing.SEARCH, 1)]
    assert billing.events_for("extract", {}, {"results": [1, 2, 3]}) == [(billing.PAGE_READ, 3)]
    assert billing.events_for("crawl", {}, {"results": []}) == []
    assert billing.events_for("map", {}, {"results": ["a"]}) == [(billing.SITE_MAP, 1)]
    assert billing.events_for("research", {}, {}) == []


class FakeActor:
    """Stands in for apify.Actor: records charges, can pretend the budget is used up."""

    def __init__(self):
        self.charges = []
        self.over_budget = False
        actor = self

        class Manager:
            def is_event_charge_limit_reached(self, name):
                return actor.over_budget

        self._manager = Manager()

    def get_charging_manager(self):
        return self._manager

    async def charge(self, event_name, count=1):
        self.charges.append((event_name, count))


@pytest.fixture()
def hosted(tmp_path, monkeypatch):
    import deepsift.actor as A
    fake_actor = FakeActor()
    monkeypatch.setattr(A, "Actor", fake_actor)
    monkeypatch.setattr(E.httpx, "get", lambda *a, **kw: httpx.Response(
        200, text=SAMPLE, request=httpx.Request("GET", "http://www.google.com/search")))
    service = Service(_settings(tmp_path))
    service.fetcher.fetch_many = lambda urls, **kw: [
        P.Page(url=u, title="Page", text="Intro.\n\nVector databases store embeddings for similarity search.")
        for u in urls
    ]
    with TestClient(A.build_web_app(service)) as client:
        client.actor = fake_actor
        yield client


def test_hosted_search_is_tavily_shaped_and_charged(hosted):
    r = hosted.get("/", headers={"x-apify-container-server-readiness-probe": "1"})
    assert r.status_code == 200
    r = hosted.post("/search", json={"query": "vector databases", "max_results": 2, "include_answer": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["results"]) == 2 and body["answer"] is None   # no LLM on hosted copies
    assert "embeddings" in body["results"][0]["content"]
    assert body["engine"] == "google"
    assert hosted.actor.charges == [("search", 1)]
    r = hosted.post("/research", json={"input": "anything"})
    assert r.status_code == 400 and hosted.actor.charges == [("search", 1)]


def test_hosted_failures_and_budget_are_not_charged(hosted):
    assert hosted.post("/search", json={"query": ""}).status_code == 400
    hosted.actor.over_budget = True
    r = hosted.post("/search", json={"query": "vector databases"})
    assert r.status_code == 402 and "spending limit" in r.json()["detail"]["error"]
    assert hosted.actor.charges == []


def _mcp(client, method, params=None, id_=1):
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}},
                    headers={"Accept": "application/json, text/event-stream",
                             "MCP-Protocol-Version": "2025-06-18", "Host": "x.apify.actor"})
    assert r.status_code == 200, r.text
    return r.json()


def test_hosted_mcp_tools_charge_per_call(hosted):
    _mcp(hosted, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "1"}})
    tools = _mcp(hosted, "tools/list", id_=2)["result"]["tools"]
    assert sorted(t["name"] for t in tools) == ["web_crawl", "web_map", "web_read", "web_search"]
    # Directories (Claude, OpenAI, Glama, M8ven) want a title and all four hints on every tool.
    for t in tools:
        assert t["title"] and t["annotations"] == {
            "title": t["title"], "readOnlyHint": True, "destructiveHint": False,
            "idempotentHint": True, "openWorldHint": True}, t
    out = _mcp(hosted, "tools/call", {"name": "web_search", "arguments": {"query": "vector databases",
                                                                           "max_results": 2}}, id_=3)
    payload = json.loads(out["result"]["content"][0]["text"])
    assert len(payload["results"]) == 2
    assert hosted.actor.charges == [("search", 1)]


def test_batch_input_mapping():
    import deepsift.actor as A
    body = A.search_body({"searchDepth": "advanced", "maxResults": 3, "timeRange": "month",
                          "includeDomains": [], "includeFullPages": True}, "q1")
    assert body == {"query": "q1", "search_depth": "advanced", "max_results": 3, "topic": "general",
                    "include_published_date": True, "time_range": "month", "include_raw_content": "markdown"}
    rows = A.result_rows("q1", {"results": [{"title": "T", "url": "https://a.com", "content": "c", "score": 0.9,
                                             "published_date": None, "raw_content": "full"}]})
    assert rows == [{"query": "q1", "rank": 1, "title": "T", "url": "https://a.com", "content": "c",
                     "score": 0.9, "publishedDate": None, "fullPage": "full"}]


def test_real_google_page_with_encrypted_links():
    """A real results page (Nov 2026) whose links are all encrypted /goto redirects."""
    import gzip
    html = gzip.decompress((HERE / "google_real_goto.html.gz").read_bytes()).decode()
    hits = E.parse_google_html(html)
    assert len(hits) >= 8
    assert all(h.url.startswith("https://www.google.com/goto?url=") for h in hits)
    assert all(h.title for h in hits) and sum(bool(h.snippet) for h in hits) >= 6


def test_goto_links_resolve_to_real_addresses(monkeypatch):
    targets = {"AAA": "https://redis.io/blog/vdb/", "BBB": None, "CCC": "https://redis.io/blog/vdb/"}

    def fake_get(url, **kw):
        assert kw.get("follow_redirects") is False and "proxy" not in kw  # straight to google.com
        target = targets[url.rsplit("=", 1)[1]]
        headers = {"location": target} if target else {}
        return httpx.Response(302 if target else 403, headers=headers, request=httpx.Request("GET", url))

    monkeypatch.setattr(E.httpx, "get", fake_get)
    hits = [E.Hit("A", "https://www.google.com/goto?url=AAA"), E.Hit("B", "https://www.google.com/goto?url=BBB"),
            E.Hit("C", "https://example.com/"), E.Hit("A again", "https://www.google.com/goto?url=CCC")]
    out = E.resolve_goto_links(hits, "ua")
    # unresolvable B dropped, duplicate of A dropped, plain link kept in order
    assert [(h.title, h.url) for h in out] == [("A", "https://redis.io/blog/vdb/"), ("C", "https://example.com/")]


def test_pdf_reading_stops_at_the_time_limit(monkeypatch):
    import pypdf

    clock = iter(range(0, 1000, 3))  # every page "takes" 3 seconds
    monkeypatch.setattr(P.time, "monotonic", lambda: next(clock))

    class FakePage:
        def __init__(self, n):
            self.n = n

        def extract_text(self):
            return "page %d" % self.n

    monkeypatch.setattr(pypdf, "PdfReader", lambda stream: type("R", (), {"pages": [FakePage(i) for i in range(300)]})())
    assert P._pdf_text(b"%PDF-", seconds=7).split("\n\n") == ["page 0", "page 1", "page 2"]
    assert len(P._pdf_text(b"%PDF-").split("\n\n")) == 200  # no limit: the first 200 pages
