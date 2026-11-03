"""`deepsift` — the same abilities from any terminal (Desktop Commander, scripts).

  deepsift search "databricks unity catalog pricing" [-n 6] [--advanced] [--answer] [--recent week]
  deepsift read https://example.com/a https://example.com/b [--focus "pricing"]
  deepsift map https://docs.example.com [--depth 2]
  deepsift crawl https://docs.example.com [--instructions "pricing"] [--limit 15]
  deepsift research "question" [--thorough]
Add --json to any command for the raw Tavily-style reply.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import config
from .engines import EngineError
from .service import BadRequest, Service


def _print_results(reply: dict) -> None:
    if reply.get("answer"):
        print("ANSWER\n" + reply["answer"] + "\n")
    for i, row in enumerate(reply.get("results", []), 1):
        if isinstance(row, str):
            print(row)
            continue
        print("%d. %s" % (i, row.get("title") or row.get("url")))
        print("   " + row["url"] + ("   (" + row["published_date"] + ")" if row.get("published_date") else ""))
        text = row.get("content") or row.get("raw_content") or ""
        print("   " + text.replace("\n", "\n   ") + "\n")
    for row in reply.get("failed_results", []):
        print("FAILED %s: %s" % (row["url"], row["error"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="deepsift", description="Local web search, read, crawl and research.")
    parser.add_argument("--json", action="store_true", help="print the raw reply")
    sub = parser.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("-n", type=int, default=6)
    s.add_argument("--advanced", action="store_true")
    s.add_argument("--fast", action="store_true")
    s.add_argument("--answer", action="store_true")
    s.add_argument("--recent", choices=["day", "week", "month", "year"])
    s.add_argument("--site", action="append", help="only this domain (repeatable)")
    s.add_argument("--json", action="store_true", dest="json_sub")

    r = sub.add_parser("read")
    r.add_argument("urls", nargs="+")
    r.add_argument("--focus")
    r.add_argument("--json", action="store_true", dest="json_sub")

    m = sub.add_parser("map")
    m.add_argument("url")
    m.add_argument("--depth", type=int, default=1)
    m.add_argument("--limit", type=int, default=50)
    m.add_argument("--json", action="store_true", dest="json_sub")

    c = sub.add_parser("crawl")
    c.add_argument("url")
    c.add_argument("--instructions")
    c.add_argument("--depth", type=int, default=1)
    c.add_argument("--limit", type=int, default=15)
    c.add_argument("--json", action="store_true", dest="json_sub")

    q = sub.add_parser("research")
    q.add_argument("question")
    q.add_argument("--thorough", action="store_true")
    q.add_argument("--json", action="store_true", dest="json_sub")

    args = parser.parse_args(argv)
    raw = args.json or getattr(args, "json_sub", False)
    svc = Service(config.load())
    try:
        if args.command == "search":
            depth = "advanced" if args.advanced else ("fast" if args.fast else "basic")
            body = {"query": args.query, "max_results": args.n, "search_depth": depth,
                    "include_answer": args.answer, "include_published_date": True}
            if args.recent:
                body["time_range"] = args.recent
            if args.site:
                body["include_domains"] = args.site
            reply = svc.search(body)
        elif args.command == "read":
            body = {"urls": args.urls}
            if args.focus:
                body["query"] = args.focus
            reply = svc.extract(body)
        elif args.command == "map":
            reply = svc.map({"url": args.url, "max_depth": args.depth, "limit": args.limit, "allow_external": False})
        elif args.command == "crawl":
            body = {"url": args.url, "max_depth": args.depth, "limit": args.limit, "allow_external": False}
            if args.instructions:
                body["instructions"] = args.instructions
            reply = svc.crawl(body)
        else:
            job = svc.start_research({"input": args.question, "model": "pro" if args.thorough else "mini"})
            print("Researching (this takes a few minutes)...", file=sys.stderr)
            while True:
                reply = svc.research_status(job["request_id"])
                if reply["status"] in ("completed", "failed"):
                    break
                time.sleep(3)
    except (BadRequest, EngineError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if raw:
        print(json.dumps(reply, indent=2))
    elif args.command == "research":
        if reply["status"] == "failed":
            print("error: " + reply.get("error", "research failed"), file=sys.stderr)
            return 1
        content = reply.get("content")
        print(content if isinstance(content, str) else json.dumps(content, indent=2))
        print("\nSOURCES")
        for i, src in enumerate(reply.get("sources", []), 1):
            print("[%d] %s %s" % (i, src.get("title", ""), src["url"]))
    else:
        _print_results(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
