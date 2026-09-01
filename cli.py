"""Command line interface."""
from __future__ import annotations

import argparse
import sys

from . import build, query
from .config import Config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser("repomap", description="Build a searchable mental map of a repo.")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out", default=None, help="default: <repo>/docs/map")
    ap.add_argument("--model", default="claude-sonnet-4-6")
    ap.add_argument("--no-llm", action="store_true",
                    help="facts only; skeleton map with no prose")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="build or refresh the whole map")
    b.add_argument("--since", default=None, help="e.g. '2 years ago'")
    b.add_argument("--max-modules", type=int, default=400)

    sub.add_parser("overview")
    m = sub.add_parser("module"); m.add_argument("name")
    h = sub.add_parser("history"); h.add_argument("name")
    f = sub.add_parser("flow"); f.add_argument("name")
    s = sub.add_parser("search"); s.add_argument("term")
    c = sub.add_parser("callers"); c.add_argument("symbol")
    i = sub.add_parser("introduced"); i.add_argument("snippet")

    d = sub.add_parser("pr", help="explain the current diff for review")
    d.add_argument("--base", default="origin/main")
    d.add_argument("--diff-file", default=None, help="read a diff from a file instead")

    sub.add_parser("serve", help="run the MCP server on stdio")

    a = ap.parse_args(argv)
    cfg = Config.from_env(a.repo, a.out, model=a.model, enable_llm=not a.no_llm,
                          since=getattr(a, "since", None),
                          max_modules=getattr(a, "max_modules", 400))

    if a.cmd == "build":
        stats = build.build_all(cfg)
        print(f"Map written to {cfg.out}")
        for k, v in stats.items():
            print(f"  {k}: {v}")
    elif a.cmd == "overview":
        print(query.overview(cfg))
    elif a.cmd == "module":
        print(query.module(cfg, a.name))
    elif a.cmd == "history":
        print(query.history(cfg, a.name))
    elif a.cmd == "flow":
        print(query.flow(cfg, a.name))
    elif a.cmd == "search":
        print("\n".join(query.search(cfg, a.term)) or "no matches")
    elif a.cmd == "callers":
        print("\n".join(query.callers(cfg, a.symbol)) or "no matches")
    elif a.cmd == "introduced":
        print("\n".join(query.introduced_by(cfg, a.snippet)) or "no matches")
    elif a.cmd == "pr":
        text = open(a.diff_file).read() if a.diff_file else None
        print(query.explain_diff(cfg, text, a.base))
    elif a.cmd == "serve":
        from .mcp_server import serve
        serve(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
