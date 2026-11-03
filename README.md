# DeepSift

**A free, self-hosted alternative to Tavily for Claude Code.** Ask one question; DeepSift searches the web,
reads every page, and hands back only the paragraphs that matter. It runs on your laptop and uses the Claude
subscription you already pay for.

[Tavily](https://www.tavily.com) is a paid web search API built for AI agents. You send one question; it
searches the web, opens the best pages, cuts out the passages that answer the question, and sends it all
back in one tidy reply. That's great, but every call costs credits.

DeepSift does the same job on your own computer, in three steps:

1. **Find** the right websites, using Claude's web search on your existing Claude subscription
   (with free DuckDuckGo as a backup).
2. **Read** every page itself: HTML and PDFs, with menus, ads and clutter stripped out.
3. **Pick** the passages that best match your question, and give them back in one reply.

No per-call bill, no API key to buy.

## What Claude Code gets

After installing, every Claude Code session has these tools:

| Tool | What you'd ask | What it does |
|---|---|---|
| `web_search` | "web_search Databricks Unity Catalog pricing" | One topic → many websites → the best passages from each, in one reply |
| `web_read` | "read these 3 pages and focus on pricing" | Full text of up to 20 pages or PDFs, or only the parts about a topic |
| `web_map` | "list all pages on docs.example.com" | A site's page addresses |
| `web_crawl` | "read the pricing pages on example.com" | Reads many pages of one site, keeps the relevant ones |
| `web_research` | "research the top 5 vector databases with sources" | A multi-search investigation that returns a written report with numbered sources (takes minutes) |

The same abilities work from any terminal:

```sh
deepsift search "Databricks Unity Catalog pricing" --advanced
deepsift read https://example.com/page --focus "pricing"
deepsift map https://docs.example.com
deepsift crawl https://docs.example.com --instructions "pricing"
deepsift research "Which vector databases support hybrid search?"
```

## Install (about 1 minute)

You need a Mac or Linux computer with [Claude Code](https://claude.com/claude-code) and Python 3.10+.

```sh
git clone https://github.com/pavanmulka/deepsift.git
cd deepsift
./install.sh
claude auth login        # only if you aren't signed in to Claude Code's command line yet
```

Open a new Claude Code session and ask it to use `web_search`. That's it.

`./uninstall.sh` removes it from Claude Code again.

## How it compares to Tavily

| | Tavily | DeepSift |
|---|---|---|
| Cost | Pay per credit | Free (uses your Claude plan) |
| Speed per search | 1–2 s | ~10–30 s through Claude; ~1–3 s on the DuckDuckGo backup |
| Search, extract, crawl, map, research | ✓ | ✓ |
| Same request/response format | — | ✓ (optional local HTTP API, see below) |
| Runs where | Their servers | Your laptop |

**Limits, honestly:**
- Searches through Claude count toward your Claude plan's usage limits (3 at a time at most).
  Repeat searches within 24 h come from a local cache.
- DuckDuckGo pauses you after a burst of quick searches; the tool then rests it for 15 minutes.
- Pages that only show their text after JavaScript runs come back thin.
- It's meant for **your own personal use** with your own Claude subscription. To power an app for other
  people, use a proper paid API.

## Optional: a local Tavily-compatible API

`./run.sh` starts an HTTP server on `127.0.0.1:8787` with Tavily's paths and fields (`/search`, `/extract`,
`/crawl`, `/map`, `/research`, `GET /research/{id}`, `/usage`). Code written for Tavily can point its base
URL there. Every reply reports `usage.credits: 0`.

## Settings

Copy `.env.example` to `.env` to change engines, models, cache time, or set an API key for the HTTP server.

## How it works

```
question ─► find sites (Claude WebSearch via `claude -p`, or DuckDuckGo)
         ─► download pages in parallel (httpx) ─► readable text (trafilatura, pypdf)
         ─► split into passages ─► rank by relevance to the question (BM25) ─► one reply
```

`claude -p` always runs on your subscription sign-in. `ANTHROPIC_API_KEY` is removed before it starts,
so it never bills an API account by accident.

## Tests

```sh
.venv/bin/python -m pytest -q tests
```

The tests run offline, using a stand-in for `claude`.

## License

MIT
