"""The AI scorer's wiring (a fake model stands in; the real one needs fastembed + a download)."""
from deepsift import pages as P
from deepsift.config import Settings
from deepsift.engines import Hit
from deepsift.rerank import clean_question, to_unit
from deepsift.service import Service


class FakeReranker:
    """Scores a passage by how many of the question's words it holds; 'boom' breaks it."""
    def __init__(self, broken=False):
        self.broken, self.calls = broken, []

    def scores(self, question, passages):
        if self.broken:
            raise RuntimeError("model missing")
        self.calls.append((question, list(passages)))
        words = set(question.lower().split())
        return [float(sum(w in p.lower() for w in words)) * 3 - 3 for p in passages]


class Engine:
    name = "google"

    def __init__(self, hits):
        self.hits = hits

    def search(self, q):
        return list(self.hits)


def make(tmp_path, reranker, hits, pages):
    s = Service(Settings(backends=["ddg"], data_dir=tmp_path, llm_enabled=False))
    s.engines = [Engine(hits)]
    s.reranker = reranker
    s.fetcher.fetch_many = lambda urls, **kw: [pages[u] for u in urls if u in pages]
    return s


def test_clean_question():
    assert clean_question('site:linkedin.com/in/ "Teladoc Health" "CFO" OR "VP Finance"') == "Teladoc Health CFO VP Finance"


def test_to_unit():
    assert to_unit(0) == 0.5 and to_unit(10) > 0.99 and to_unit(-10) < 0.01


def test_ai_scorer_reorders_results_and_picks_the_best_passage(tmp_path):
    hits = [Hit(title="Pizza", url="https://pizza.com", snippet="Best pizza in town"),
            Hit(title="Teladoc leadership", url="https://t.com/leaders", snippet="Our team")]
    pages = {
        "https://pizza.com": P.Page(url="https://pizza.com", title="Pizza", text="Cheese and dough.\\n\\nOpen late."),
        "https://t.com/leaders": P.Page(url="https://t.com/leaders", title="Leaders",
                                        text="We care for patients.\n\nMark Hirschhorn is the Teladoc chief financial officer."),
    }
    fake = FakeReranker()
    out = make(tmp_path, fake, hits, pages).search({"query": '"Teladoc" chief financial officer', "max_results": 2})
    urls = [r["url"] for r in out["results"]]
    assert urls[0] == "https://t.com/leaders"
    assert "Hirschhorn" in out["results"][0]["content"]
    assert out["results"][0]["score"] > out["results"][1]["score"]
    assert fake.calls[0][0] == "Teladoc chief financial officer"


def test_broken_scorer_falls_back_to_word_ranking(tmp_path):
    hits = [Hit(title="A", url="https://a.com", snippet="alpha")]
    out = make(tmp_path, FakeReranker(broken=True), hits, {}).search({"query": "alpha"})
    assert [r["url"] for r in out["results"]] == ["https://a.com"]
