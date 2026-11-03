import importlib
import json
import os
import pathlib
import sys
import time

import pytest

from deepsift import pages as P
from deepsift.claude_cli import parse_json_text
from deepsift.engines import parse_ddg_html

HERE = pathlib.Path(__file__).parent


@pytest.fixture()
def client(tmp_path, monkeypatch):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, HERE / "fake_claude.py"))
    fake.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("DEEPSIFT_CLAUDE_BIN", str(fake))
    monkeypatch.setenv("DEEPSIFT_BACKENDS", "claude")
    monkeypatch.setenv("DEEPSIFT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DEEPSIFT_API_KEY", "local-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-reach-claude")
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    from fastapi.testclient import TestClient
    import deepsift.app as app_module
    app_module = importlib.reload(app_module)
    # Keep tests offline: pages come from a stub instead of the network.
    app_module.service.fetcher.fetch_many = lambda urls, **kw: [
        P.Page(url=u, title="Example", text="Intro line.\n\nThe example domain is used in docs. "
               "It is reserved for illustrative examples.", published_date="2024-01-02")
        for u in urls
    ]
    c = TestClient(app_module.app)
    c.calls = log
    return c


AUTH = {"Authorization": "Bearer local-key"}


def test_search_matches_tavily_shape(client):
    r = client.post("/search", headers=AUTH, json={
        "query": "example domain docs", "include_answer": True, "include_usage": True,
        "include_published_date": True, "include_favicon": True})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "example domain docs"
    assert body["answer"] == "A short answer."
    assert body["usage"] == {"credits": 0}
    assert body["request_id"]
    [row] = body["results"]  # the javascript: URL is dropped
    assert row["url"] == "https://example.com/"
    assert "example domain" in row["content"].lower()
    assert row["published_date"] == "2024-01-02"
    assert row["favicon"].startswith("https://example.com/")
    assert 0 < row["score"] <= 1
    calls = [json.loads(line) for line in client.calls.read_text().splitlines()]
    assert all(not c["has_api_key"] for c in calls), "API key would bill the API instead of the subscription"
    assert "--tools" in calls[0]["args"] and "WebSearch" in calls[0]["args"]


def test_auth_header_or_body_key(client):
    assert client.post("/search", json={"query": "x"}).status_code == 401
    assert client.post("/search", json={"query": "x", "api_key": "local-key"}).status_code == 200


def test_errors_use_tavily_detail_shape(client):
    r = client.post("/search", headers=AUTH, json={"query": "x", "search_depth": "deep"})
    assert r.status_code == 400 and "error" in r.json()["detail"]
    r = client.post("/extract", headers=AUTH, json={"urls": ["https://a.com"] * 21, "include_usage": True})
    assert r.status_code == 400
    # Cost-tracking callers can settle a failed call at zero.
    assert r.json()["usage"] == {"credits": 0} and r.json()["request_id"]


def test_search_cache_and_max_age(client):
    body = {"query": "cache me"}
    client.post("/search", headers=AUTH, json=body)
    client.post("/search", headers=AUTH, json=body)
    assert len(client.calls.read_text().splitlines()) == 1
    client.post("/search", headers=AUTH, json=dict(body, max_age_hours=0))
    assert len(client.calls.read_text().splitlines()) == 2


def test_domain_filters(client):
    r = client.post("/search", headers=AUTH, json={"query": "q", "exclude_domains": ["example.com"]})
    assert r.json()["results"] == []
    r = client.post("/search", headers=AUTH, json={"query": "q2", "include_domains": ["other.org"]})
    assert r.json()["results"] == []


def test_research_job_lifecycle(client):
    r = client.post("/research", headers=AUTH, json={"input": "When was Databricks founded?", "model": "mini"})
    assert r.status_code == 201
    rid = r.json()["request_id"]
    for _ in range(50):
        job = client.get("/research/" + rid, headers=AUTH).json()
        if job["status"] in ("completed", "failed"):
            break
        time.sleep(0.1)
    assert job["status"] == "completed"
    assert "2013" in job["content"]
    assert job["sources"][0]["url"].startswith("https://en.wikipedia.org")
    usage = client.get("/usage", headers=AUTH).json()
    assert usage["local"]["claude_web_searches"] >= 3


def test_extract_with_query_reranks(client):
    r = client.post("/extract", headers=AUTH, json={"urls": "https://example.com", "query": "reserved", "chunks_per_source": 1})
    [row] = r.json()["results"]
    assert "reserved" in row["raw_content"]


def test_ranking_helpers():
    cats, price, dogs = ("Cats sleep a lot. " * 20).strip(), "Pricing is per DBU hour on Databricks. " * 10, ("Dogs bark. " * 40).strip()
    text = "\n\n".join([cats, price.strip(), dogs])
    [best] = P.best_chunks("databricks pricing", text, 1)
    assert "DBU" in best and "Cats" not in best
    assert P.domain_matches("https://docs.databricks.com/x", ["databricks.com"])
    assert not P.domain_matches("https://notdatabricks.com", ["databricks.com"])
    assert P.phrase_hits('"unity catalog" price', ["Unity Catalog costs", "unity price"]) == [True, False]


def test_parse_json_text():
    assert parse_json_text('Sure:\n```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_text('noise [1, 2] tail') == [1, 2]


def test_parse_ddg_html():
    markup = (HERE / "ddg_sample.html").read_text()
    hits = parse_ddg_html(markup)
    assert [h.url for h in hits] == ["https://docs.databricks.com/unity", "https://example.org/b"]
    assert hits[0].snippet == "Unity Catalog docs."
