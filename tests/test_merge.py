"""Several engines at once (DEEPSIFT_MERGE), Google retries, SearXNG site: filtering."""
import httpx
import pytest

from deepsift import engines as E
from deepsift.config import Settings
from deepsift.engines import EngineError, Hit, Query
from deepsift.service import Service, _url_key


class Fake:
    def __init__(self, name, hits=None, error=None):
        self.name, self._hits, self._error = name, hits or [], error

    def search(self, q):
        if self._error:
            raise EngineError(self._error)
        return list(self._hits)


def hit(url, snippet="s"):
    return Hit(title=url, url=url, snippet=snippet)


def service(tmp_path, engines, merge=True):
    s = Service(Settings(backends=["ddg"], merge_engines=merge, data_dir=tmp_path, llm_enabled=False))
    s.engines = engines
    return s


def test_merge_ranks_links_both_engines_found_first(tmp_path):
    a = Fake("google", [hit("https://a.com/1"), hit("https://www.both.com/x/")])
    b = Fake("searxng", [hit("https://both.com/x"), hit("https://b.com/2")])
    hits, name = service(tmp_path, [a, b])._run_engines(Query(text="q"))
    assert [h.url for h in hits][:1] == ["https://www.both.com/x/"]
    assert {h.url for h in hits} == {"https://a.com/1", "https://www.both.com/x/", "https://b.com/2"}
    assert name == "google+searxng"


def test_merge_keeps_going_when_one_engine_fails(tmp_path):
    a = Fake("google", error="google: timed out")
    b = Fake("searxng", [hit("https://b.com/2")])
    hits, name = service(tmp_path, [a, b])._run_engines(Query(text="q"))
    assert [h.url for h in hits] == ["https://b.com/2"] and name == "searxng"


def test_merge_errors_only_when_every_engine_fails(tmp_path):
    a = Fake("google", error="google: timed out")
    b = Fake("searxng", error="searxng: down")
    with pytest.raises(EngineError):
        service(tmp_path, [a, b])._run_engines(Query(text="q"))
    hits, _ = service(tmp_path, [Fake("google"), Fake("searxng")])._run_engines(Query(text="q"))
    assert hits == []
    # one engine down, the other honestly found nothing: an empty answer, not an error
    hits, name = service(tmp_path, [Fake("google", error="google: timed out"), Fake("searxng")])._run_engines(Query(text="q"))
    assert hits == [] and name == "searxng"


def test_without_merge_first_engine_still_wins(tmp_path):
    a = Fake("google", [hit("https://a.com/1")])
    b = Fake("searxng", [hit("https://b.com/2")])
    hits, name = service(tmp_path, [a, b], merge=False)._run_engines(Query(text="q"))
    assert [h.url for h in hits] == ["https://a.com/1"] and name == "google"


def test_url_key():
    assert _url_key("https://www.A.com/x/") == _url_key("http://a.com/x") == "a.com/x"
    assert _url_key("https://a.com/x?id=1") != _url_key("https://a.com/x?id=2")


class Resp:
    def __init__(self, text="", status=200):
        self.text, self.status_code = text, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("bad", request=None, response=None)


GOOGLE_PAGE = '<a href="/url?q=https://x.com/a"><h3>X</h3></a><div class="VwiC3b">snip</div>'


def test_google_tries_again_after_a_timeout(monkeypatch):
    calls = []

    def fake_get(*a, **kw):
        calls.append(kw["timeout"])
        if len(calls) == 1:
            raise httpx.ReadTimeout("timed out")
        return Resp(GOOGLE_PAGE)

    monkeypatch.setattr(E.httpx, "get", fake_get)
    monkeypatch.setattr(E, "resolve_goto_links", lambda hits, ua: hits)
    engine = E.ApifySerpEngine(Settings(serp_proxy_url="http://p", google_timeout=8, google_retries=1))
    assert [h.url for h in engine.search(Query(text="x"))] == ["https://x.com/a"]
    assert calls == [8, 8]


def test_google_tries_again_after_an_empty_page(monkeypatch):
    pages = iter([Resp("<html>nothing</html>"), Resp(GOOGLE_PAGE)])
    monkeypatch.setattr(E.httpx, "get", lambda *a, **kw: next(pages))
    monkeypatch.setattr(E, "resolve_goto_links", lambda hits, ua: hits)
    engine = E.ApifySerpEngine(Settings(serp_proxy_url="http://p", google_retries=1))
    assert len(engine.search(Query(text="x"))) == 1


def test_google_without_retries_fails_on_first_timeout(monkeypatch):
    def boom(*a, **kw):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(E.httpx, "get", boom)
    engine = E.ApifySerpEngine(Settings(serp_proxy_url="http://p"))
    with pytest.raises(EngineError):
        engine.search(Query(text="x"))


class JsonResp(Resp):
    def __init__(self, rows):
        super().__init__()
        self._rows = rows

    def json(self):
        return {"results": self._rows}


def test_searxng_keeps_site_searches_on_the_site_and_asks_chosen_engines(monkeypatch):
    seen = []

    def fake_get(url, params, timeout):
        seen.append(dict(params))
        if params["pageno"] == "1":
            return JsonResp([{"url": "https://www.linkedin.com/in/jane", "title": "Jane"},
                             {"url": "https://example.com/other", "title": "Other"}])
        return JsonResp([{"url": "https://uk.linkedin.com/in/joe", "title": "Joe"}])

    monkeypatch.setattr(E.httpx, "get", fake_get)
    engine = E.SearxngEngine(Settings(searxng_url="http://sx", searxng_engines="yahoo"))
    hits = engine.search(Query(text='site:linkedin.com/in/ "Acme" CFO', max_results=8))
    assert [h.url for h in hits] == ["https://www.linkedin.com/in/jane", "https://uk.linkedin.com/in/joe"]
    assert seen[0]["engines"] == "yahoo" and len(seen) == 2


class Counting(Fake):
    def __init__(self, name, by_text):
        super().__init__(name)
        self.by_text, self.asked = by_text, []

    def search(self, q):
        self.asked.append(q.text)
        return list(self.by_text.get(q.text, []))


def test_too_few_results_tries_again_without_quotes(tmp_path):
    strict = 'site:linkedin.com/in/ "Acme (Ayer, MA)" "CFO"'
    loose = "site:linkedin.com/in/ Acme Ayer, MA CFO"
    a = Counting("google", {strict: [hit("https://linkedin.com/in/a")], loose: [hit("https://linkedin.com/in/b")]})
    b = Counting("searxng", {loose: [hit("https://linkedin.com/in/a"), hit("https://linkedin.com/in/c")]})
    hits, _ = service(tmp_path, [a, b])._run_engines(Query(text=strict, max_results=8))
    assert [h.url for h in hits][0] == "https://linkedin.com/in/a"
    assert {h.url for h in hits} == {"https://linkedin.com/in/a", "https://linkedin.com/in/b", "https://linkedin.com/in/c"}
    assert a.asked == [strict, loose]


def test_enough_results_means_no_second_round(tmp_path):
    a = Counting("google", {'"x"': [hit("https://a.com/%d" % i) for i in range(5)]})
    b = Counting("searxng", {})
    service(tmp_path, [a, b])._run_engines(Query(text='"x"', max_results=8))
    assert a.asked == ['"x"']
