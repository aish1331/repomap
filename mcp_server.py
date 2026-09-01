"""MCP server exposing the map as callable tools.

Implemented directly against the JSON-RPC/stdio wire format so the tool has no
runtime dependencies. If you already use the official `mcp` package, swap this
file for it -- the tool bodies are unchanged.

The point of serving the map rather than dumping it: an agent reviewing a PR
pulls the three nodes it needs instead of loading the whole map into context.
"""
from __future__ import annotations

import json
import sys

from . import build, query
from .config import Config

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {"name": "map_overview",
     "description": "Top-level map of the repository: modules, roles, flows, sizes. "
                    "Call this first when unfamiliar with the codebase.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "map_module",
     "description": "What one module is for, its public surface, entry points, "
                    "dependencies and blast radius. Accepts a module name or a file path.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "map_history",
     "description": "How a feature evolved: episodes, decisions, reverts, and what "
                    "was tried and abandoned. Use when code looks odd and you want "
                    "to know why it is the way it is.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "map_flow",
     "description": "One named execution flow end to end, with a diagram.",
     "inputSchema": {"type": "object",
                     "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "map_search",
     "description": "Full-text search across the map.",
     "inputSchema": {"type": "object",
                     "properties": {"term": {"type": "string"}}, "required": ["term"]}},
    {"name": "map_callers",
     "description": "Find references to a symbol in the working tree (ground truth).",
     "inputSchema": {"type": "object",
                     "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "map_introduced_by",
     "description": "Which commit introduced or removed a given code string (git pickaxe).",
     "inputSchema": {"type": "object",
                     "properties": {"snippet": {"type": "string"}}, "required": ["snippet"]}},
    {"name": "map_explain_diff",
     "description": "Review context for a PR: modules touched, what they do, blast "
                    "radius, and the prior commits that touched the same lines.",
     "inputSchema": {"type": "object", "properties": {
         "base": {"type": "string", "description": "base ref, default origin/main"},
         "diff": {"type": "string", "description": "optional unified diff text"}}}},
    {"name": "map_rebuild",
     "description": "Rebuild the map. Only changed modules and new commits cost tokens.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def _dispatch(cfg: Config, name: str, args: dict) -> str:
    if name == "map_overview":
        return query.overview(cfg)
    if name == "map_module":
        return query.module(cfg, args["name"])
    if name == "map_history":
        return query.history(cfg, args["name"])
    if name == "map_flow":
        return query.flow(cfg, args["name"])
    if name == "map_search":
        return "\n".join(query.search(cfg, args["term"])) or "no matches"
    if name == "map_callers":
        return "\n".join(query.callers(cfg, args["symbol"])) or "no matches"
    if name == "map_introduced_by":
        return "\n".join(query.introduced_by(cfg, args["snippet"])) or "no matches"
    if name == "map_explain_diff":
        return query.explain_diff(cfg, args.get("diff"), args.get("base", "origin/main"))
    if name == "map_rebuild":
        return json.dumps(build.build_all(cfg), indent=2)
    raise ValueError(f"unknown tool: {name}")


def _reply(id_, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": id_}
    if error:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def serve(cfg: Config) -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid, params = req.get("method"), req.get("id"), req.get("params", {})

        if method == "initialize":
            _reply(rid, {"protocolVersion": PROTOCOL_VERSION,
                         "capabilities": {"tools": {}},
                         "serverInfo": {"name": "repomap", "version": "0.1.0"}})
        elif method == "tools/list":
            _reply(rid, {"tools": TOOLS})
        elif method == "tools/call":
            try:
                text = _dispatch(cfg, params.get("name", ""), params.get("arguments") or {})
                _reply(rid, {"content": [{"type": "text", "text": text}]})
            except Exception as e:  # surfaced to the agent, not swallowed
                _reply(rid, {"content": [{"type": "text", "text": f"error: {e}"}],
                             "isError": True})
        elif rid is not None:
            _reply(rid, {}) if method.startswith("notifications/") else \
                _reply(rid, error={"code": -32601, "message": f"unknown method {method}"})
