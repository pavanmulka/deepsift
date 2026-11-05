from deepsift.config import Settings
from deepsift.engines import ApifySerpEngine, Hit, Query, only_sites, site_targets


def hit(url):
    return Hit(title="t", url=url, snippet="s")


def test_site_targets():
    assert site_targets('site:linkedin.com/in/ "Jane" "Kroger"') == [("linkedin.com", "/in")]
    assert site_targets("site:www.example.com pricing") == [("example.com", "")]
    assert site_targets("kroger -site:youtube.com") == []
    assert site_targets("no operator here") == []


def test_only_sites_keeps_matching_host_and_path():
    hits = [hit("https://www.linkedin.com/in/jane"), hit("https://www.linkedin.com/posts/x"),
            hit("https://uk.linkedin.com/in/joe"), hit("https://www.globaldata.com/kroger"),
            hit("https://notlinkedin.com/in/x")]
    kept = only_sites(hits, site_targets("site:linkedin.com/in/ jane"))
    assert [h.url for h in kept] == ["https://www.linkedin.com/in/jane", "https://uk.linkedin.com/in/joe"]
    assert only_sites(hits, []) == hits


def test_site_search_uses_the_plain_web_view():
    engine = ApifySerpEngine(Settings(serp_proxy_url="http://proxy"))
    assert engine._params(Query(text="site:linkedin.com/in/ jane"), 0)["udm"] == "14"
    assert engine._params(Query(text="jane", include_domains=["a.com"]), 0)["udm"] == "14"
    assert "udm" not in engine._params(Query(text="jane"), 0)
    news = engine._params(Query(text="site:a.com jane", topic="news"), 0)
    assert news["tbm"] == "nws" and "udm" not in news
