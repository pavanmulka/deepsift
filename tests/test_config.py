from deepsift import config


def test_serp_proxy_url_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("DEEPSIFT_SERP_PROXY_URL", "http://groups-GOOGLE_SERP:secret@proxy.apify.com:8000")
    monkeypatch.setenv("DEEPSIFT_BACKENDS", "google")
    settings = config.load()
    assert settings.serp_proxy_url == "http://groups-GOOGLE_SERP:secret@proxy.apify.com:8000"
    assert settings.backends == ["google"]


def test_no_serp_proxy_by_default(monkeypatch):
    monkeypatch.delenv("DEEPSIFT_SERP_PROXY_URL", raising=False)
    assert config.load().serp_proxy_url is None
