"""Settings, read once from the environment."""
from __future__ import annotations

import os
import pathlib
from dataclasses import dataclass, field
from typing import List, Optional


def _list(value: str) -> List[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


@dataclass(frozen=True)
class Settings:
    # Search engines tried in order until one returns results.
    backends: List[str] = field(default_factory=lambda: ["claude", "ddg"])
    # True: ask every engine at once and merge their results (links found by several engines rank
    # higher) instead of stopping at the first engine that answers.
    merge_engines: bool = False
    # Optional self-hosted SearXNG (e.g. http://127.0.0.1:8888). Add "searxng" to backends.
    searxng_url: Optional[str] = None
    # Which of SearXNG's engines to ask (e.g. "yahoo"); empty = the instance's own defaults.
    searxng_engines: str = ""
    # Required bearer key. Empty means no key check (fine on 127.0.0.1 only).
    api_key: str = ""
    # Google search through Apify's GOOGLE_SERP proxy. The Apify Actor sets it itself; a self-hosted
    # server sets DEEPSIFT_SERP_PROXY_URL (http://groups-GOOGLE_SERP:<proxy password>@proxy.apify.com:8000).
    serp_proxy_url: Optional[str] = None
    # Seconds to wait for one Google results page, and how many more tries after a timeout or an
    # empty page (failed proxy requests are not billed).
    google_timeout: float = 20.0
    google_retries: int = 0
    # Optional AI scorer that runs on this machine (needs `fastembed`), e.g.
    # Xenova/ms-marco-MiniLM-L-6-v2. Empty = word-count ranking only.
    reranker: str = ""
    reranker_cache: str = ""
    # False on hosted copies: no `claude -p`, so no written answers and no research reports.
    llm_enabled: bool = True
    claude_bin: str = "claude"
    search_model: str = "haiku"      # fast model for the search step
    answer_model: str = "haiku"      # include_answer=basic
    advanced_model: str = "sonnet"   # include_answer=advanced, research mini
    research_model: str = "opus"     # research pro
    claude_concurrency: int = 3
    claude_timeout: float = 120.0
    fetch_concurrency: int = 12
    cache_hours: float = 24.0
    data_dir: pathlib.Path = pathlib.Path.home() / ".deepsift"
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    )


PROJECT_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_dotenv(path: pathlib.Path) -> None:
    """Apps that launch the tools (Claude Code, Codex) don't read .env, so read it here."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


def load() -> Settings:
    _load_dotenv(PROJECT_DIR / ".env")
    env = os.environ
    defaults = Settings()
    return Settings(
        backends=_list(env.get("DEEPSIFT_BACKENDS", "")) or defaults.backends,
        merge_engines=env.get("DEEPSIFT_MERGE", "0").lower() in ("1", "true", "yes", "on"),
        searxng_url=env.get("DEEPSIFT_SEARXNG_URL") or None,
        searxng_engines=env.get("DEEPSIFT_SEARXNG_ENGINES", ""),
        serp_proxy_url=env.get("DEEPSIFT_SERP_PROXY_URL") or None,
        google_timeout=float(env.get("DEEPSIFT_GOOGLE_TIMEOUT", defaults.google_timeout)),
        reranker=env.get("DEEPSIFT_RERANKER", ""),
        reranker_cache=env.get("DEEPSIFT_RERANKER_CACHE", ""),
        google_retries=int(env.get("DEEPSIFT_GOOGLE_RETRIES", defaults.google_retries)),
        api_key=env.get("DEEPSIFT_API_KEY", ""),
        llm_enabled=env.get("DEEPSIFT_LLM", "1").lower() not in ("0", "false", "no", "off"),
        claude_bin=env.get("DEEPSIFT_CLAUDE_BIN", defaults.claude_bin),
        search_model=env.get("DEEPSIFT_MODEL", defaults.search_model),
        answer_model=env.get("DEEPSIFT_ANSWER_MODEL", defaults.answer_model),
        advanced_model=env.get("DEEPSIFT_ADVANCED_MODEL", defaults.advanced_model),
        research_model=env.get("DEEPSIFT_RESEARCH_MODEL", defaults.research_model),
        claude_concurrency=int(env.get("DEEPSIFT_CLAUDE_CONCURRENCY", defaults.claude_concurrency)),
        claude_timeout=float(env.get("DEEPSIFT_CLAUDE_TIMEOUT", defaults.claude_timeout)),
        fetch_concurrency=int(env.get("DEEPSIFT_FETCH_CONCURRENCY", defaults.fetch_concurrency)),
        cache_hours=float(env.get("DEEPSIFT_CACHE_HOURS", defaults.cache_hours)),
        data_dir=pathlib.Path(env.get("DEEPSIFT_DATA_DIR", str(defaults.data_dir))).expanduser(),
    )
