"""The built-in Yahoo engine (used next to Google by the Apify Actor) and the Yahoo-only loose round."""
import gzip
import pathlib
import time

import httpx
import pytest

from deepsift import engines as E
from deepsift.config import Settings
from deepsift.engines import EngineError, Hit, Query
from deepsift.service import Service

HERE = pathlib.Path(__file__).parent
SAMPLE = gzip.open(HERE / "yahoo_sample.html.gz", "rt").read()


def test_parse_real_yahoo_page():
    hits = E.parse_yahoo_html(SAMPLE)
    assert len(hits) == 7
    first = hits[0]
    assert first.url == "https://www.datastackhub.com/top-tools/open-source-vector-database-tools/"  # RU= unwrapped
    assert first.title == "8 Best Open Source Vector Database Tools for 2026"
    assert first.snippet.startswith("Compare 8 open source vector database tools")  # date taken off
    assert first.published_date == "2026-08-24"
    assert all("yahoo.com" not in h.url for h in hits)


def test_yahoo_target_keeps_plain_links():
    assert E._yahoo_target("https://r.search.yahoo.com/_ylt=x/RU=https%3a%2f%2fa.com%2fb/RK=2/RS=z") == "https://a.com/b"
    assert E._yahoo_target("https://a.com/") == "https://a.com/"


class Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("Server error '500'", request=None, response=None)


def block(url, title):
    return ('<div class="dd algo algo-sr Sr"><div class="compTitle"><a href="https://r.search.yahoo.com/x'
            '/RU=%s/RK=2/RS=y"><h3>%s</h3></a></div><div class="compText"><p>about %s</p></div></div>'
            % (url.replace(":", "%3a").replace("/", "%2f"), title, title))


def test_yahoo_request_filters_sites_and_reads_page_two_only_when_needed(monkeypatch):
    calls = []
    page1 = "".join(block("https://www.linkedin.com/in/p%d" % i, "P%d" % i) for i in range(6)) + \
        block("https://example.com/x", "Other")
    pages = iter([Resp(page1), Resp(block("https://uk.linkedin.com/in/joe", "Joe"))])

    def fake_get(url, params, **kw):
        calls.append((url, dict(params), kw.get("proxy")))
        return next(pages)

    monkeypatch.setattr(E.httpx, "get", fake_get)
    engine = E.YahooEngine(Settings())
    hits = engine.search(Query(text='site:linkedin.com/in/ "Acme" CFO', max_results=11, time_range="week",
                               exclude_domains=["spam.com"]))
    assert [h.url for h in hits][-1] == "https://uk.linkedin.com/in/joe"
    assert "https://example.com/x" not in [h.url for h in hits]
    assert calls[0][0] == "https://search.yahoo.com/search" and calls[0][2] is None  # plain request, no proxy
    assert calls[0][1]["btf"] == "w" and "-site:spam.com" in calls[0][1]["p"]
    assert calls[1][1]["b"] == "8" and engine.pages_fetched == 2

    calls.clear()
    pages = iter([Resp(page1)])
    E.YahooEngine(Settings()).search(Query(text="acme", max_results=5))
    assert len(calls) == 1  # 5 wanted: one page is enough


def test_yahoo_server_error_gets_exactly_one_more_try(monkeypatch):
    calls = []

    def flaky(*a, **kw):
        calls.append(1)
        return Resp(status=500) if len(calls) == 1 else Resp(text=SAMPLE)

    monkeypatch.setattr(E.httpx, "get", flaky)
    monkeypatch.setattr(E.YahooEngine, "RETRY_PAUSE", 0)
    hits = E.YahooEngine(Settings()).search(Query(text="x", max_results=5))
    assert len(calls) == 2 and hits

    calls.clear()
    monkeypatch.setattr(E.httpx, "get", lambda *a, **kw: calls.append(1) or Resp(status=500))
    with pytest.raises(EngineError):
        E.YahooEngine(Settings()).search(Query(text="x"))
    assert calls == [1, 1]  # two server errors: give up, never a third try


@pytest.mark.parametrize("status", [403, 429])
def test_yahoo_refusal_is_an_error_and_never_retried(monkeypatch, status):
    calls = []

    def refuse(*a, **kw):
        calls.append(1)
        return Resp(status=status)

    monkeypatch.setattr(E.httpx, "get", refuse)
    with pytest.raises(EngineError):
        E.YahooEngine(Settings()).search(Query(text="x"))
    assert calls == [1]


def test_read_more_link_text_is_dropped_from_previews():
    assert E._clean_snippet("Exa Search starts at $4 per 1,000 requests ...Read more") == \
        "Exa Search starts at $4 per 1,000 requests ..."
    assert E._clean_snippet("Prices went up. Read more") == "Prices went up."
    assert E._clean_snippet("Read more about it") == "Read more about it"


class Fake:
    def __init__(self, name, hits):
        self.name, self._hits, self.queries = name, hits, []

    def search(self, q):
        self.queries.append(q.text)
        return list(self._hits)


def test_loose_round_asks_only_the_chosen_engines(tmp_path):
    s = Service(Settings(backends=["ddg"], merge_engines=True, loose_backends=["yahoo"], data_dir=tmp_path,
                         llm_enabled=False))
    google = Fake("google", [Hit(title="a", url="https://a.com")])
    yahoo = Fake("yahoo", [Hit(title="b", url="https://b.com")])
    s.engines = [google, yahoo]
    hits, name = s._run_engines(Query(text='"Acme (Ayer, MA)" CFO', max_results=8))
    assert google.queries == ['"Acme (Ayer, MA)" CFO']  # one Google page only
    assert yahoo.queries == ['"Acme (Ayer, MA)" CFO', "Acme Ayer, MA CFO"]
    assert name == "google+yahoo"


class Slow(Fake):
    def __init__(self, name, hits, seconds, fail=False):
        super().__init__(name, hits)
        self.seconds, self.fail = seconds, fail

    def search(self, q):
        time.sleep(self.seconds)
        if self.fail:
            raise EngineError(self.name + ": 500")
        return super().search(q)


def merging(tmp_path, patience):
    return Service(Settings(backends=["ddg"], merge_engines=True, merge_patience=patience, data_dir=tmp_path,
                            llm_enabled=False))


def test_slow_engine_is_left_behind_once_another_has_results(tmp_path):
    s = merging(tmp_path, 0.2)
    s.engines = [Slow("google", [Hit(title="g", url="https://g.com")], 1.5),
                 Slow("yahoo", [Hit(title="y", url="https://y.com")], 0.0)]
    started = time.monotonic()
    hits, name = s._merge_engines(Query(text="acme", max_results=5))
    assert time.monotonic() - started < 1.0
    assert [h.url for h in hits] == ["https://y.com"] and name == "yahoo"


def test_slow_engine_is_waited_for_when_nothing_else_came_back(tmp_path):
    s = merging(tmp_path, 0.1)
    s.engines = [Slow("google", [Hit(title="g", url="https://g.com")], 0.5),
                 Slow("yahoo", [], 0.0, fail=True)]
    hits, name = s._merge_engines(Query(text="acme", max_results=5))
    assert [h.url for h in hits] == ["https://g.com"] and name == "google"


def test_without_patience_every_engine_is_waited_for(tmp_path):
    s = merging(tmp_path, 0)
    s.engines = [Slow("google", [Hit(title="g", url="https://g.com")], 0.3),
                 Slow("yahoo", [Hit(title="y", url="https://y.com")], 0.0)]
    hits, name = s._merge_engines(Query(text="acme", max_results=5))
    assert name == "google+yahoo" and len(hits) == 2


def test_build_engines_knows_yahoo():
    engines = E.build_engines(Settings(backends=["google", "yahoo"], serp_proxy_url="http://p"), runner=None)
    assert [e.name for e in engines] == ["google", "yahoo"]
